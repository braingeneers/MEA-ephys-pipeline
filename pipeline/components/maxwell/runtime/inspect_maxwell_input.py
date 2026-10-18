"""Inspect frozen fresh Maxwell inputs without decoding raw samples or staging S3 files.

This produces input evidence, not an artifact envelope or a complete scratch plan.
Scientific defaults and routing helpers are supplied by the pinned owning image.
"""
from contextlib import contextmanager
import copy
import math
import os
from pathlib import Path
import re

import h5py

from ephys_modular.contracts import (ContractError, MAX_JSON_BYTES, canonical_json,
                                    decode_json, digest, validate_ref)
from ephys_modular.local import LocalStore
from ephys_modular.s3 import S3Error, S3Store
from frozen_s3_file import FrozenS3File
from maxwell_h5_utils import list_routed_raw_dataset_paths, raw_dataset_path_for_well
from workflow_defaults import scientific_default


class HeaderError(ContractError):
    pass


def _parts(path):
    parts = [part for part in path.split('/') if part and part != '.']
    if '..' in parts:
        raise HeaderError('Unsupported parent component in an HDF5 link')
    return parts


def _resolve(handle, path, depth=0):
    """Resolve internal links before HDF5 can follow an undeclared external file."""
    if depth > 32:
        raise HeaderError('Cyclic or overly deep HDF5 soft link')
    parts = _parts(path)
    current = handle['/']
    for index, part in enumerate(parts):
        if not isinstance(current, h5py.Group):
            raise HeaderError('HDF5 link traverses a dataset')
        link = current.get(part, getlink=True)
        if link is None:
            raise HeaderError('Dangling HDF5 link in fresh input')
        if isinstance(link, h5py.ExternalLink):
            raise HeaderError('Fresh input has an undeclared external HDF5 dependency')
        if isinstance(link, h5py.SoftLink):
            target = (link.path if link.path.startswith('/') else
                      '/' + '/'.join(parts[:index] + [link.path]))
            return _resolve(handle, target + '/' + '/'.join(parts[index + 1:]), depth + 1)
        if not isinstance(link, h5py.HardLink):
            raise HeaderError('Unsupported HDF5 link in fresh input')
        current = current[part]
    return current


def _token(value):
    # Native IDs hash and compare by HDF5 object identity, including aliases.
    # Full H5O info also calculates chunk-index statistics; on a recorded file
    # that reads scattered index nodes unrelated to the header facts we need.
    # Keep these keys inside the open-file lifetime, never in a JSON report.
    return value.id


def _inventory(handle, max_objects, max_links):
    for value in (max_objects, max_links):
        if type(value) is not int or value <= 0:
            raise HeaderError('HDF5 traversal budgets must be positive integers')
    seen, datasets, objects, pending, links = set(), {}, [], [('/', handle['/'])], 0
    link_name_bytes = 0
    while pending:
        path, value = pending.pop()
        token = _token(value)
        if token in seen:
            continue
        seen.add(token)
        if len(seen) > max_objects:
            raise HeaderError('HDF5 object budget exhausted')
        objects.append((token, path, value))
        if isinstance(value, h5py.Group):
            for name in value:
                links += 1
                link_name_bytes += len(name.encode('utf-8'))
                if links > max_links:
                    raise HeaderError('HDF5 link budget exhausted')
                child = path.rstrip('/') + '/' + name
                pending.append((child, _resolve(handle, child)))
        elif isinstance(value, h5py.Dataset):
            if value.is_virtual or value.external:
                raise HeaderError('Fresh input contains VDS/external storage; use verified recovery/adoption')
            logical = (0 if value.shape is None else None if value.dtype.hasobject
                       else math.prod(value.shape) * value.dtype.itemsize)
            datasets[token] = {'path': path, 'shape': list(value.shape) if value.shape is not None else None,
                               'dtype': value.dtype.str,
                               'fixed_logical_bytes': logical}
        else:
            raise HeaderError('Unsupported named HDF5 object in fresh input')
    return datasets, {'unique_objects': len(seen), 'links': links}, objects, link_name_bytes


def _scalar(handle, path):
    if path not in handle:
        raise HeaderError('Missing Maxwell scalar: ' + path)
    value = handle[path]
    if (not isinstance(value, h5py.Dataset) or value.shape is None or math.prod(value.shape) != 1
            or value.dtype.kind not in 'iuf'):
        raise HeaderError('Expected one finite positive numeric Maxwell scalar: ' + path)
    number = float(value[()].reshape(-1)[0])
    if not math.isfinite(number) or number <= 0:
        raise HeaderError('Expected one finite positive numeric Maxwell scalar: ' + path)
    return number


def _well_path(handle, selector, raw_paths):
    well = selector.split(':', 1)[1].strip().lower()
    if not re.fullmatch(r'well\d+', well):
        raise HeaderError('Invalid Maxwell well selector')
    matched = {}
    for root in ('recordings', 'wells'):
        if root not in handle:
            continue
        if not isinstance(handle[root], h5py.Group):
            raise HeaderError('Malformed Maxwell routing group')
        if root == 'wells':
            groups = handle[root][well].values() if well in handle[root] else ()
        else:
            groups = (recording[well] for recording in handle[root].values()
                      if isinstance(recording, h5py.Group) and well in recording)
        for group in groups:
            for raw in raw_paths:
                if handle[raw.rsplit('/groups/routed/raw', 1)[0]].id == group.id:
                    matched[_token(handle[raw])] = raw
    if len(matched) > 1:
        raise HeaderError('Well selects multiple raw tables; supply an explicit dataset path')
    # Use the same maintained resolver as conversion. Its numeric fallback is
    # accepted only when independently supported by stored well identity.
    try:
        result = raw_dataset_path_for_well(handle, well)
    except ValueError as error:
        if len(matched) == 1:
            # Files may expose only wells links. The actual stored object identity
            # is authoritative even when the older resolver cannot find it.
            result = next(iter(matched.values()))
        else:
            raise HeaderError(str(error)) from None
    group = result.rsplit('/groups/routed/raw', 1)[0]
    if matched:
        if _token(handle[result]) not in matched:
            raise HeaderError('Well routing contradicts the selected raw table')
    else:
        identity = group + '/well_id'
        if identity not in handle or int(_scalar_well(handle, identity)) != int(well[4:]):
            raise HeaderError('Well routing lacks matching stored identity; supply an explicit raw path')
    identity = group + '/well_id'
    if identity in handle and int(_scalar_well(handle, identity)) != int(well[4:]):
        raise HeaderError('Stored well identity contradicts recording links')
    return result


def _scalar_well(handle, path):
    value = handle[path]
    if (not isinstance(value, h5py.Dataset) or value.shape is None
            or math.prod(value.shape) != 1 or value.dtype.kind not in 'iu'):
        raise HeaderError('Malformed Maxwell well identity')
    return value[()].reshape(-1)[0]


def inspect_header(handle, selectors, *, max_objects=50000, max_links=100000,
                   metadata_read_budget_bytes=None):
    datasets, traversal, objects, link_name_bytes = _inventory(handle, max_objects, max_links)
    raw_paths = list_routed_raw_dataset_paths(handle)
    if not raw_paths:
        raise HeaderError('No routed Maxwell raw table found')
    if any(not isinstance(handle[path], h5py.Dataset) for path in raw_paths):
        raise HeaderError('Routed raw path is not a dataset')
    raw_tokens = {_token(handle[path]) for path in raw_paths}
    selected, tokens = [], set()
    for requested in selectors:
        if not isinstance(requested, str) or not requested.strip():
            raise HeaderError('Maxwell selectors must be nonempty strings')
        selector = requested.strip()
        if selector.lower() == 'auto':
            if len(raw_tokens) != 1:
                raise HeaderError('Multiple raw tables require an explicit well/dataset selector')
            path = raw_paths[0]
        elif selector.lower().startswith('well:'):
            path = _well_path(handle, selector, raw_paths)
        else:
            path = '/' + selector.lstrip('/')
            if path not in raw_paths:
                raise HeaderError('Selected path is not a routed Maxwell raw table')
        raw = handle[path]
        token = _token(raw)
        if token in tokens:
            raise HeaderError('Selection names the same raw table more than once')
        tokens.add(token)
        if raw.ndim != 2 or any(size <= 0 for size in raw.shape) or raw.dtype.kind not in 'iuf':
            raise HeaderError('Expected a nonempty two-dimensional numeric Maxwell raw table')
        group = path.rsplit('/groups/routed/raw', 1)[0]
        properties = raw.id.get_create_plist()
        selected.append({'selector': requested, 'raw_dataset_path': path,
                         'shape': list(raw.shape), 'dtype': raw.dtype.str,
                         'logical_bytes': math.prod(raw.shape) * raw.dtype.itemsize,
                         'chunks': list(raw.chunks) if raw.chunks else None,
                         'filter_ids': [int(properties.get_filter(i)[0]) for i in range(properties.get_nfilters())],
                         'sampling_hz': _scalar(handle, group + '/settings/sampling'),
                         'lsb': _scalar(handle, group + '/settings/lsb')})
    metadata = [value for token, value in datasets.items() if token not in raw_tokens]
    raw_volumes = [datasets[token]['fixed_logical_bytes'] for token in raw_tokens]
    result = {'selected': selected, 'raw_table_count': len(raw_tokens),
            'raw_path_count': len(raw_paths),
            'selected_raw_logical_bytes': sum(unit['logical_bytes'] for unit in selected),
            'all_raw_logical_bytes': sum(raw_volumes) if all(value is not None for value in raw_volumes) else None,
            'non_raw_fixed_logical_bytes': sum(item['fixed_logical_bytes'] or 0 for item in metadata),
            'variable_length_metadata_paths': sorted(item['path'] for item in metadata
                                                    if item['fixed_logical_bytes'] is None),
            'traversal': traversal, 'fresh_input_self_contained': True}
    metadata_metrics = None
    if metadata_read_budget_bytes is not None:
        from metadata_volume import measure_metadata
        result['metadata_volume'], metadata_metrics = measure_metadata(
            objects, raw_tokens, read_budget_bytes=metadata_read_budget_bytes)
        result['metadata_volume'].update(link_count=traversal['links'], link_name_bytes=link_name_bytes)
        result['metadata_volume']['total_payload_bytes'] += link_name_bytes
    return result, metadata_metrics


@contextmanager
def _input(store, reference, budgets):
    if reference['uri'].startswith('file:'):
        if getattr(store, 'transport', 's3') != 'local':
            raise HeaderError('Local input inspection must explicitly select local transport')
        with store.open_verified(reference) as stream:
            yield stream
    else:
        store.verify(reference)
        with FrozenS3File(store, reference, **budgets) as stream:
            try:
                yield stream
                store.verify(reference)
            except BaseException as error:
                # The caller's native HDF5 context has already unwound. Release
                # failed constructor/callback frames before closing its backing
                # stream, preserving the original error and diagnostic stack.
                stream.release_error_frames(error)
                raise


def inspect_input(store, reference, selectors=None, *, read_budgets=None,
                  max_objects=50000, max_links=100000, metadata_read_budget_bytes=None):
    """Return immutable input/header facts; never mutate source or publish science."""
    validate_ref(reference, transport=getattr(store, 'transport', 's3'))
    reference = copy.deepcopy(reference)
    selectors = [scientific_default('maxwell_raw_dataset_path')] if selectors is None else selectors
    if not isinstance(selectors, (list, tuple)) or not 0 < len(selectors) <= 1024:
        raise HeaderError('Supply a bounded nonempty selector list')
    with _input(store, reference, read_budgets or {}) as stream:
        try:
            with h5py.File(stream, 'r') as handle:
                header, metadata_metrics = inspect_header(handle, selectors,
                    max_objects=max_objects, max_links=max_links,
                    metadata_read_budget_bytes=metadata_read_budget_bytes)
        except Exception:
            # Some HDF5 callback paths wrap read errors. Preserve the common
            # transfer exception/exit 76, rather than disguising it as bad science.
            if isinstance(stream, FrozenS3File) and stream.failure is not None:
                raise stream.failure from None
            raise
        metrics = (dict(stream.metrics(), full_sha256_verified=False)
                   if isinstance(stream, FrozenS3File) else {'full_sha256_verified': True})
        if metadata_metrics is not None:
            metrics['metadata'] = metadata_metrics
    report = {'schema_version': 1, 'document_type': 'maxwell_input_header',
              'source': reference, 'header': header,
              'header_digest': digest({'source': reference, 'header': header}),
              'inspection': metrics, 'complete_scratch_plan': False}
    canonical_json(report)
    return report


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-reference-file', required=True)
    parser.add_argument('--output-file', required=True)
    parser.add_argument('--selector', action='append')
    parser.add_argument('--transport', choices=('s3', 'local'), default='s3')
    parser.add_argument('--s3-endpoint')
    parser.add_argument('--aws-profile')
    args = parser.parse_args()
    with open(args.source_reference_file, 'rb') as stream:
        reference = decode_json(stream.read(MAX_JSON_BYTES + 1))
    validate_ref(reference, transport=args.transport)
    store_type = LocalStore if args.transport == 'local' else S3Store
    store = store_type(endpoint=args.s3_endpoint, profile=args.aws_profile)
    report = inspect_input(store, reference, args.selector)
    target = Path(os.path.abspath(args.output_file))
    output = LocalStore(target.parent.as_uri() + '/')
    reference = output.put_json(target.as_uri(), report)
    print(reference['uri'])


if __name__ == '__main__':
    try:
        main()
    except S3Error as error:
        import sys
        print('[inspect_maxwell_input] ' + str(error), file=sys.stderr)
        raise SystemExit(error.exit_code) from None
    except ContractError as error:
        import sys
        print('[inspect_maxwell_input] ' + str(error), file=sys.stderr)
        raise SystemExit(2) from None
