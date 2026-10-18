"""Publish a frozen raw-selection envelope from complete direct recordings."""
import copy
import hashlib
import os
from pathlib import Path
import re

from ephys_modular.contracts import (ContractError, EPHYS, MAX_JSON_BYTES, _shape,
    assert_within, canonical_json, decode_json, digest, make_stage, manifest_uri,
    new_envelope, object_ref, parse_file, parse_s3, validate_ref, validate_root)
from ephys_modular.local import LocalStore
from ephys_modular.publication import publish_initial, publish_stage
from ephys_modular.s3 import IntegrityError, S3Error, S3Store
from inspect_maxwell_input import inspect_input
from task_preparation import _projection_metadata_size
from workflow_defaults import scientific_default


def input_location(value):
    if not isinstance(value, str) or not value.strip():
        raise ContractError('Recording/projection inputs require explicit file or S3 locations')
    if value.startswith('s3:'):
        parse_s3(value)
        return value
    if value.startswith('file:'):
        parse_file(value)
        return value
    if '://' in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ContractError('Only explicit local files and S3 inputs are supported')
    return Path(os.path.abspath(value)).as_uri()


def source_basename(uri):
    return parse_file(uri).name if uri.startswith('file:') else parse_s3(uri)[1].rsplit('/', 1)[-1]


def recording_identity(uri):
    """Retain the maintained URI-based identity; never use a staged cache URI."""
    name = source_basename(uri)
    if not name.endswith('.raw.h5'):
        raise ContractError('A Maxwell recording must identify one .raw.h5 object')
    stem = name[:-len('.raw.h5')]
    identity = re.sub(r'[^A-Za-z0-9_.-]', '_',
                      stem + '-' + hashlib.sha256(uri.encode()).hexdigest()[:12])
    _shape(identity, 'identifier')
    return identity


def _wells(value):
    if value is None:
        return []
    if isinstance(value, str):
        values = [item.strip() for item in value.split(',') if item.strip()]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = [item.strip() for item in value if item.strip()]
    else:
        raise ContractError('Uploaded wells/well_filter must be a list or comma-separated string')
    if (len(values) != len(set(values))
            or any(not re.fullmatch(r'well[0-9]{3}', item) for item in values)):
        raise ContractError('Max Two wells must be unique names such as well002')
    return values


def _freeze_source(reader, uri, supplied=None):
    if supplied is None:
        return reader.stat(uri)
    validate_ref(supplied, transport='local')
    if supplied['uri'] != uri:
        raise ContractError('Frozen source reference differs from the original input location')
    reader.verify(supplied)
    return copy.deepcopy(supplied)


def _freeze_projection(reader, specification):
    if specification is None or (isinstance(specification, dict) and not specification):
        return None
    if (not isinstance(specification, dict) or not isinstance(specification.get('uri'), str)
            or not isinstance(specification.get('sha256'), str)
            or not re.fullmatch(r'[a-f0-9]{64}', specification['sha256'])):
        raise ContractError('Per-unit metadata requires both a location and lowercase SHA-256')
    uri = input_location(specification['uri'])
    known = {key: specification[key] for key in ('size', 'etag', 'sha256', 'version_id')
             if key in specification}
    if 'version_id' in known and not {'size', 'etag'}.issubset(known):
        raise ContractError('Versioned projection inputs require a complete frozen object reference')
    if {'size', 'etag'}.issubset(known):
        ref = dict(known, uri=uri)
        validate_ref(ref, strong=True, transport='local')
    else:
        ref = reader.stat(uri)
        for key in ('size', 'etag'):
            if key in known and known[key] != ref[key]:
                raise IntegrityError('Projection identity differs from the supplied input')
        if ref.get('sha256', known['sha256']) != known['sha256']:
            raise IntegrityError('Projection SHA-256 differs from the supplied input')
        ref = dict(ref, sha256=known['sha256'])
    value, observed = reader.read_json(uri, expected=ref)
    _projection_metadata_size(value)
    return observed


def prepare_raw_selection(store, recordings, context, owner, *, read_budgets=None):
    """Read-only validation; no cache, selection or scientific output is written."""
    transport = getattr(store, 'transport', 's3')
    context, owner, recordings = copy.deepcopy(context), copy.deepcopy(owner), copy.deepcopy(recordings)
    root, collection = context['output_base_uri'], context['collection_uri']
    validate_root(root, transport)
    validate_root(collection, transport)
    if store.output_root != root:
        raise ContractError('Raw-selection context differs from the reviewed output root')
    assert_within(collection + 'planned-object', root, transport=transport)
    manifest_uri(root, context['run_id'], context['step_id'], transport=transport)
    _shape(owner, 'owner')
    if (owner['workflow_id'] != 'braingeneers-modular-maxwell-to-nwb'
            or context.get('workflow_version', owner['workflow_version']) != owner['workflow_version']):
        raise ContractError('Raw selection requires the owning Maxwell workflow/version')
    if not isinstance(recordings, list) or not recordings or any(not isinstance(r, dict) for r in recordings):
        raise ContractError('recordings must be a nonempty list of complete recording dictionaries')
    canonical_json(recordings)
    reader = store if transport == 'local' else LocalStore(s3_store=store)
    identities, source_uris, units_seen, prepared = set(), set(), set(), []
    for index, recording in enumerate(recordings):
        uri = input_location(recording.get('input_uri'))
        identity = recording_identity(uri)
        if uri in source_uris or identity in identities:
            raise ContractError('Each original recording input must occur exactly once')
        source_uris.add(uri)
        identities.add(identity)
        data_format = recording.get('data_format', '')
        if not isinstance(data_format, str):
            raise ContractError('Recording data_format must name Maxwell/Max One/Max Two')
        data_format = data_format.lower()
        if data_format == 'max2':
            data_format = 'maxtwo'
        if data_format not in ('maxwell', 'maxone', 'maxtwo'):
            raise ContractError('Recording data_format must name Maxwell/Max One/Max Two')
        inventory, filtered = _wells(recording.get('wells')), _wells(recording.get('well_filter'))
        if inventory and filtered and not set(filtered).issubset(inventory):
            raise ContractError('well_filter selects a well outside the uploaded inventory')
        names = (filtered or inventory) if data_format == 'maxtwo' else ['recording']
        if not names:
            raise ContractError('Max Two recording must declare its selected uploaded wells')
        selector = recording.get('raw_dataset_path', scientific_default('maxwell_raw_dataset_path'))
        if not isinstance(selector, str) or not selector.strip():
            raise ContractError('raw_dataset_path must be an explicit selector or auto')
        if data_format == 'maxtwo' and selector.strip().lower() != 'auto':
            raise ContractError('Max Two well selection cannot be replaced by a different raw selector')
        selectors = ['well:' + name for name in names] if data_format == 'maxtwo' else [selector]
        metadata = recording.get('nwb_metadata', {})
        if not isinstance(metadata, dict):
            raise ContractError('nwb_metadata must map unit names to frozen projection references')
        source = _freeze_source(reader, uri, recording.get('source_reference'))
        header = inspect_input(reader, source, selectors, read_budgets=read_budgets)
        units = []
        for unit_index, (name, selected) in enumerate(zip(names, header['header']['selected'])):
            unit_id = re.sub(r'[^A-Za-z0-9_.-]', '_', identity + '-' + name)
            _shape(unit_id, 'identifier')
            if unit_id in units_seen:
                raise ContractError('Selected units have conflicting original identities')
            units_seen.add(unit_id)
            output = collection + 'outputs/' + identity + '/' + name + '/'
            validate_root(output, transport)
            assert_within(output + 'planned-object', root, transport=transport)
            projection = _freeze_projection(reader, metadata.get(name))
            units.append({'unit_id': unit_id, 'unit_name': name, 'unit_index': unit_index,
                'raw_dataset_path': selectors[unit_index],
                'resolved_raw_dataset_path': selected['raw_dataset_path'],
                'unit_output_uri': output, 'original_projection': projection})
        cached = [source] + [unit['original_projection'] for unit in units if unit['original_projection']]
        needs_cache = transport == 's3' and any(ref['uri'].startswith('file:') for ref in cached)
        cache_base = None
        if needs_cache:
            cache_root = context.get('cache_uri')
            validate_root(cache_root, 's3')
            cache_base = recording.get('cache_uri') or cache_root
            if not isinstance(cache_base, str):
                raise ContractError('Reviewed recording cache prefix must be an S3 location')
            cache_base = cache_base.rstrip('/') + '/'
            validate_root(cache_base, 's3')
            assert_within(cache_base + 'planned-object', cache_root)
            cache_base += ('modular-v1/' + context['run_id'] + '/' + context['step_id']
                           + '/' + identity + '/')
        prepared.append({'recording_index': index, 'recording_id': identity, 'original_source': source,
            'source_basename': source_basename(uri), 'source_header_digest': header['header_digest'],
            'source_full_sha256_verified': header['inspection']['full_sha256_verified'],
            'units': units, 'cache_base_uri': cache_base})
    return {'recordings': recordings, 'context': context, 'owner': owner, 'records': prepared}


def publish_raw_selection(store, recordings, context, owner, *, cache_store=None, read_budgets=None):
    """Publish one common raw graph; full scientific execution remains worker-owned."""
    plan = prepare_raw_selection(store, recordings, context, owner, read_budgets=read_budgets)
    transport = getattr(store, 'transport', 's3')
    context = plan['context']
    needs_cache = any(record['cache_base_uri'] for record in plan['records'])
    if needs_cache:
        if cache_store is None:
            cache_store = S3Store(context['cache_uri'], client=store.client, policy=store.policy)
        if cache_store.output_root != context['cache_uri'] or cache_store.transport != 's3':
            raise ContractError('Cache store differs from the explicitly reviewed S3 cache root')
    reader = store if transport == 'local' else LocalStore(s3_store=store)
    cached_refs = {}
    def stage(reference, record, kind):
        if reference is None or transport != 's3' or not reference['uri'].startswith('file:'):
            return reference, None
        uri = (record['cache_base_uri'] + kind + '/' + digest(reference)
               + '/' + source_basename(reference['uri']))
        if uri in cached_refs:
            return cached_refs[uri]
        with reader.open_verified(reference):
            staged = cache_store.upload_file(uri, parse_file(reference['uri']))
            observed = cache_store.verify_contents(staged)
            if observed['sha256'] != reference['sha256'] or observed['size'] != reference['size']:
                raise IntegrityError('Staged cache bytes differ from their original input')
        receipt = {'schema_version': 1, 'document_type': 'maxwell_input_cache',
            'recording_id': record['recording_id'], 'original': reference,
            'staged': staged, 'full_sha256_verified': True}
        evidence = cache_store.put_json(uri + '.cache.json', receipt)
        cache_store.read_json(evidence['uri'], expected=evidence)
        cached_refs[uri] = (staged, evidence)
        return staged, evidence
    units, outcomes = [], []
    for record in plan['records']:
        raw, raw_cache = stage(record['original_source'], record, 'sources')
        for entry in record['units']:
            projection, projection_cache = stage(entry['original_projection'], record, 'projections')
            metadata = {key: record[key] for key in ('recording_index', 'source_basename',
                'source_header_digest', 'source_full_sha256_verified', 'original_source')}
            metadata.update({key: entry[key] for key in ('unit_name', 'unit_index', 'raw_dataset_path',
                                                       'resolved_raw_dataset_path', 'unit_output_uri')})
            metadata['expected_units'] = len(record['units'])
            if entry['original_projection']:
                metadata['original_projection'] = entry['original_projection']
            for key, value in (('source_cache_evidence', raw_cache), ('projection_cache_evidence', projection_cache)):
                if value is not None:
                    metadata[key] = value
            unit = {'unit_id': entry['unit_id'], 'recording_id': record['recording_id'],
                    'sources': [raw], 'metadata': metadata}
            if projection is not None:
                unit['projection'] = projection
            _shape(unit, 'unit_input')
            for ref in unit['sources']:
                validate_ref(ref, transport=transport)
            if projection is not None:
                validate_ref(projection, strong=True, transport=transport)
            units.append(unit)
            outcomes.append({'unit_id': unit['unit_id'], 'status': 'complete',
                             'artifacts': [{'kind': 'maxwell_raw', 'object': raw}]})
    root, execution = context['output_base_uri'], {key: context[key] for key in ('run_id', 'step_id')}
    uri = lambda name: manifest_uri(root, execution['run_id'], execution['step_id'], name, transport=transport)
    selection_data = canonical_json(plan['recordings'])
    selection_hash = hashlib.sha256(selection_data).hexdigest()
    placeholder = object_ref(uri('selection.json'), len(selection_data),
        'sha256-' + selection_hash if transport == 'local' else 'pending', sha256=selection_hash, transport=transport)
    # Shape/scientific binding errors must not create even an initial selection document.
    make_stage(EPHYS, 'selection', execution['step_id'], root, placeholder, plan['owner'],
               {}, {}, execution, units, outcomes, transport=transport)
    selection = store.put_json(uri('selection.json'), plan['recordings'])
    initial = new_envelope(EPHYS, root, selection, transport=transport)
    parent = publish_initial(store, initial, uri('initial.json'))
    stage_result = make_stage(EPHYS, 'selection', execution['step_id'], root, selection,
        plan['owner'], {}, {}, execution, units, outcomes, transport=transport)
    _, artifacts = publish_stage(store, initial, parent, stage_result,
        uri('result.json'), uri('artifacts.json'), {unit['unit_id'] for unit in units})
    return artifacts


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request-file', required=True)
    parser.add_argument('--transport', choices=('local', 's3'), default='s3')
    parser.add_argument('--s3-endpoint')
    parser.add_argument('--aws-profile')
    args = parser.parse_args()
    with open(args.request_file, 'rb') as stream:
        request = decode_json(stream.read(MAX_JSON_BYTES + 1))
    root = request['context']['output_base_uri']
    store = (LocalStore if args.transport == 'local' else S3Store)(
        output_root=root, endpoint=args.s3_endpoint, profile=args.aws_profile)
    result = publish_raw_selection(store, request['recordings'], request['context'], request['owner'],
                                   read_budgets=request.get('read_budgets'))
    print(result['uri'])


if __name__ == '__main__':
    try:
        main()
    except S3Error as error:
        import sys
        print('[raw_selection] ' + str(error), file=sys.stderr)
        raise SystemExit(error.exit_code) from None
    except (ContractError, ValueError) as error:
        import sys
        print('[raw_selection] ' + str(error), file=sys.stderr)
        raise SystemExit(2) from None
