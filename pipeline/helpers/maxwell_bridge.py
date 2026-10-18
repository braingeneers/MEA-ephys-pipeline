"""Bridge already-published native Maxwell conversion to portable local MEA units.

Run in the converter's pinned image with its exported runtime/common on PYTHONPATH.
Scientific completion is checked by a read-only replay of the owning commit
validator. This adapter owns local copies and bindings, not conversion science.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

from ephys_modular.contracts import (ContractError, EPHYS, MAX_JSON_BYTES, canonical_json, decode_json,
    assert_within, digest, parse_file, selected_stage)
from ephys_modular.contracts import validate_envelope, validate_stage
from ephys_modular.local import LocalStore
from ephys_modular.publication import load_envelope, load_stage
from ephys_modular.s3 import S3Error
from raw_selection import input_location
from task_preparation import prepare_conversion
from conversion_worker import commit_conversion

GIB = 1024 ** 3
PHASES = ['validate_input', 'convert', 'repack', 'companion', 'lindi',
          'validate_metadata', 'validate_outputs']
NAMES = {'nwb': 'output.nwb', 'nwb_lindi': 'output.nwb.lindi.json',
         'nwb_validation': 'output.nwb.validation.json',
         'maxwell_companion': 'output.linked.raw.h5'}


def require(condition, message):
    if not condition:
        raise ContractError(message)


class PublishedOnly(LocalStore):
    """The owning commit code may confirm existing JSON, never create/repair it."""
    def put_json(self, uri, value):
        existing, observed = self.read_json(uri)
        require(canonical_json(existing) == canonical_json(value),
                'Native publication is absent or differs; the bridge cannot repair it')
        return observed


def write_json(path, value):
    path = Path(path).absolute()
    return LocalStore(path.parent.as_uri() + '/').put_json(path.as_uri(), value)


def read_json(path):
    with Path(path).open('rb') as stream:
        return decode_json(stream.read(MAX_JSON_BYTES + 1))


def checked_plan(plan):
    require(plan.get('schema_version') == 1 and plan.get('document_type') == 'mea_maxwell_bridge_plan',
            'Unsupported conversion bridge plan')
    require(plan.get('digest') == digest({k: v for k, v in plan.items() if k != 'digest'}),
            'Changed conversion bridge plan')
    return plan


def bindings(recordings, values):
    require(isinstance(recordings, list) and recordings, 'recordings must be a nonempty list')
    require(isinstance(values, list) and values, 'Explicit unit_bindings are required')
    result, ids = {}, set()
    for value in values:
        require(isinstance(value, dict) and set(value) == {'input_uri', 'unit_name', 'metadata'},
                'A unit binding requires input_uri, unit_name and metadata')
        meta = value['metadata']
        require(isinstance(meta, dict) and isinstance(meta.get('unit_id'), str)
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', meta['unit_id']),
                'Each binding needs a stable path-safe metadata.unit_id')
        require('maxwell_conversion' not in meta, 'maxwell_conversion metadata is reserved')
        require(isinstance(value['unit_name'], str) and value['unit_name'], 'A unit_name is required')
        key = (input_location(value['input_uri']), value['unit_name'])
        require(key not in result and meta['unit_id'] not in ids, 'Conflicting conversion unit bindings')
        result[key] = copy.deepcopy(meta)
        ids.add(meta['unit_id'])
    canonical_json(recordings)
    canonical_json(values)
    return result


def prepare(reference, request):
    """Read and verify the whole native commit before creating any local unit."""
    native, wrapper = request['native'], request['wrapper']
    context = native['context']
    root = context['output_base_uri']
    parse_file(root, prefix=True)  # This portable bridge deliberately has local outputs.
    require(wrapper['publication_root'] == root + 'bridge/' + context['run_id'] + '/' + context['step_id'] + '/',
            'Bridge destination differs from its isolated native invocation')
    assert_within(wrapper['publication_root'] + 'completion.json', root, transport='local')
    require(set(request['settings']) <= set(native['defaults']), 'Unknown/replacement scientific settings')
    desired = bindings(request['recordings'], wrapper['unit_bindings'])
    store = PublishedOnly(root)
    envelope, envelope_ref = load_envelope(store, reference, root)
    records, _ = store.read_json(envelope['selection']['uri'], expected=envelope['selection'])
    require(canonical_json(records) == canonical_json(request['recordings']),
            'Native selection differs from the submitted complete recordings')
    stage_ref = selected_stage(envelope, 'conversion', transport='local')
    stage = load_stage(store, stage_ref, EPHYS, root, envelope['selection'])
    require(stage['owner'] == native['owner'] and stage['settings']['submitted'] == request['settings'],
            'Native scientific ownership/settings differ from the invocation')
    require(stage['execution'] == {k: context[k] for k in ('run_id', 'step_id')},
            'Native conversion belongs to another run or step')
    # Reconstruct the exact owning plan, including routing, projections, source
    # hashes, allocations and destinations. Reuse its complete commit validation.
    plan = prepare_conversion(store, envelope['parent'], request['settings'], context, native['owner'])
    confirmed = commit_conversion(store, plan, stage['outcomes'])
    require(confirmed == envelope_ref, 'Native completion does not bind the supplied artifact envelope')
    prepared = {task['unit']['unit_id']: task for task in plan['tasks']}
    outcomes = {outcome['unit_id']: outcome for outcome in stage['outcomes']}
    units, seen = [], set()
    for unit in stage['unit_inputs']:
        index = unit['metadata']['recording_index']
        require(type(index) is int and 0 <= index < len(records), 'Invalid original recording index')
        key = (input_location(records[index]['input_uri']), unit['metadata']['unit_name'])
        require(key in desired and key not in seen, 'Native units differ from explicit MEA bindings')
        seen.add(key)
        outcome = outcomes[unit['unit_id']]
        report, _ = store.read_json(outcome['evidence']['uri'], expected=outcome['evidence'])
        require([p['phase'] for p in report['phases']] == PHASES
                and all(p['exit_code'] == 0 for p in report['phases']), 'Incomplete native scientific phases')
        artifacts = {a['kind']: a['object'] for a in outcome['artifacts']}
        kinds = set(NAMES) if stage['settings']['effective']['create_linked_maxwell_companion'] else set(NAMES) - {'maxwell_companion'}
        require(set(artifacts) == kinds, 'Missing or unexpected native dependency siblings')
        meta = desired[key]
        destination = (wrapper['publication_root'] + 'units/' + meta['unit_id'] + '/')
        details = dict(schema_version=1, native_owner=copy.deepcopy(stage['owner']),
            wrapper_owner=copy.deepcopy(wrapper['owner']), native_unit=copy.deepcopy(unit),
            native_artifacts=envelope_ref, settings=copy.deepcopy(stage['settings']),
            artifacts=copy.deepcopy(artifacts),
            native_evidence=copy.deepcopy(outcome['evidence']),
            dependencies={kind: NAMES[kind] for kind in artifacts},
            published_root=destination, source_distribution=copy.deepcopy(native['distribution']))
        meta['maxwell_conversion'] = details
        files = {NAMES[kind]: ref for kind, ref in artifacts.items()}
        if 'projection' in unit:
            files['conversion/projection.json'] = unit['projection']
        evidence = {'conversion/recordings.json': records, 'conversion/native-artifacts.json': envelope,
            'conversion/native-stage.json': stage, 'conversion/native-unit.json': unit,
            'conversion/native-evidence.json': report}
        copied_bytes = sum(ref['size'] for ref in files.values()) + sum(len(canonical_json(v)) for v in evidence.values())
        # Scratch and create-only durable copies can coexist on one filesystem.
        requested = max(5 * GIB, ((2 * copied_bytes + GIB + 1048575) // 1048576) * 1048576)
        units.append(dict(unit_meta=meta, files=files, evidence=evidence, copied_bytes=copied_bytes,
            requested_bytes=requested, minimum_free_bytes=GIB, publication_root=destination,
            native_task_digest=digest(prepared[unit['unit_id']])))
    require(seen == set(desired), 'Every explicit unit binding must be selected exactly once')
    result = dict(schema_version=1, document_type='mea_maxwell_bridge_plan',
        native_artifacts=envelope_ref, native_stage=stage_ref, wrapper=wrapper,
        units=units, commit_disk_bytes=sum(u['requested_bytes'] for u in units))
    result['digest'] = digest(result)
    return result


def inventory(root):
    result = {}
    require(root.is_dir() and not root.is_symlink(), 'Local unit must be a real directory')
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink(), 'Local conversion units cannot contain symlinks')
        require(path.is_file() or path.is_dir(), 'Local conversion dependencies must be regular files')
        if path.is_file():
            with path.open('rb') as stream:
                result[path.relative_to(root).as_posix()] = dict(size=path.stat().st_size,
                    sha256=hashlib.file_digest(stream, 'sha256').hexdigest())
    return result


def verify_local(root, expected=None):
    """Verify a relocated unit using only its declared local dependencies."""
    root = Path(root).absolute()
    receipt = read_json(root/'conversion-unit.json')
    require(receipt.get('digest') == digest({k: v for k, v in receipt.items() if k != 'digest'}),
            'Invalid local conversion receipt')
    require(receipt.get('document_type') == 'mea_maxwell_local_unit' and receipt.get('complete') is True,
            'Local conversion is incomplete')
    actual = inventory(root)
    actual.pop('conversion-unit.json')
    require(actual == receipt['files'], 'Local conversion dependency inventory changed')
    details = receipt['unit_meta']['maxwell_conversion']
    for kind, ref in details['artifacts'].items():
        require(actual.get(NAMES[kind]) == dict(size=ref['size'], sha256=ref['sha256']),
                'Local sibling differs from its native published object')
    def frozen(relative, ref):
        require(actual.get(relative) == dict(size=ref['size'], sha256=ref['sha256']),
                'Local provenance differs from its native published object')
        return read_json(root/relative)
    envelope = frozen('conversion/native-artifacts.json', details['native_artifacts'])
    validate_envelope(envelope, transport='local')
    native_ref = selected_stage(envelope, 'conversion', transport='local')
    stage = frozen('conversion/native-stage.json', native_ref['manifest'])
    validate_stage(stage, transport='local')
    require(stage['owner'] == details['native_owner'] and stage['settings'] == details['settings'],
            'Local native ownership or settings changed')
    require(details['native_unit'] in stage['unit_inputs'], 'Local unit lost its explicit native binding')
    frozen('conversion/recordings.json', envelope['selection'])
    require(read_json(root/'conversion/native-unit.json') == details['native_unit'], 'Native unit provenance changed')
    outcomes = [o for o in stage['outcomes'] if o['unit_id'] == details['native_unit']['unit_id']]
    require(len(outcomes) == 1 and outcomes[0]['status'] == 'complete'
            and outcomes[0]['evidence'] == details['native_evidence']
            and {a['kind']: a['object'] for a in outcomes[0]['artifacts']} == details['artifacts'],
            'Local conversion lost its complete native outcome')
    report = frozen('conversion/native-evidence.json', details['native_evidence'])
    require(report.get('scientific_execution_complete') is True
            and [p['phase'] for p in report['phases']] == PHASES
            and all(p['exit_code'] == 0 for p in report['phases']), 'Local native science is incomplete')
    if 'projection' in details['native_unit']:
        frozen('conversion/projection.json', details['native_unit']['projection'])
    if expected is not None:
        require(receipt['plan_digest'] == expected[0] and receipt['unit_meta'] == expected[1]['unit_meta'],
                'Local unit differs from its frozen bridge plan')
        row = expected[1]
        require(set(actual) == set(row['files']) | set(row['evidence']), 'Local dependency coverage differs from the plan')
        for relative, ref in row['files'].items():
            require(actual[relative] == dict(size=ref['size'], sha256=ref['sha256']), 'Changed frozen local dependency')
        for relative, value in row['evidence'].items():
            require((root/relative).read_bytes() == canonical_json(value), 'Changed native provenance copy')
    # Read the actual modeled NWB and external-link companion in isolation.
    import h5py
    import ndx_cell_culture  # Register the owning typed extension.
    from pynwb import NWBHDF5IO, validate
    from cell_culture_projection import verify_cell_culture_projection
    from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
    configure_bundled_maxwell_hdf5_plugin()
    require(not validate(path=root/'output.nwb'), 'Converted NWB schema validation failed')
    projection = root/'conversion/projection.json'
    if projection.exists():
        with NWBHDF5IO(str(root/'output.nwb'), 'r', load_namespaces=True) as io:
            require(not verify_cell_culture_projection(io.read(), read_json(projection)),
                    'Converted NWB differs from its retained modeled projection')
    for filename in ('output.nwb', 'output.linked.raw.h5'):
        if not (root/filename).exists():
            continue
        with h5py.File(root/filename, 'r') as handle:
            pending, seen = [handle], set()
            while pending:
                group = pending.pop()
                if group.id in seen:
                    continue
                seen.add(group.id)
                for name in group:
                    link = group.get(name, getlink=True)
                    if isinstance(link, h5py.ExternalLink):
                        require(filename == 'output.linked.raw.h5' and link.filename == 'output.nwb',
                                'Conversion dependency escapes its local unit')
                        target = group[name]
                        require(isinstance(target, h5py.Dataset), 'Companion link is not a dataset')
                        if target.size:
                            target[tuple(0 for _ in target.shape)]
                    elif isinstance(link, h5py.HardLink) and isinstance(group[name], h5py.Group):
                        pending.append(group[name])
    return receipt


def materialize(plan, unit_id, output, requested_bytes):
    checked_plan(plan)
    rows = [u for u in plan['units'] if u['unit_meta']['unit_id'] == unit_id]
    require(len(rows) == 1, 'Unknown or conflicting bridge unit')
    row = rows[0]
    require(requested_bytes >= row['requested_bytes'], 'Insufficient requested materialization scratch')
    output = Path(output).absolute()
    require(not output.exists(), 'Materialization output must be a new directory')
    free = shutil.disk_usage(output.parent).free
    require(free >= 2 * row['copied_bytes'] + row['minimum_free_bytes'], 'Insufficient free materialization scratch')
    store = LocalStore()
    for ref in row['unit_meta']['maxwell_conversion']['native_unit']['sources']:
        store.verify_contents(ref)
    for relative, ref in row['files'].items():
        store.download(ref, output/relative)
    for relative, value in row['evidence'].items():
        write_json(output/relative, value)
    # Operational measurements are deliberately outside deterministic publication
    # identity, so an interrupted copy can safely confirm/reuse the same receipt.
    write_json(output.parent/'materialization-telemetry.json', dict(unit_id=unit_id,
        requested_bytes=requested_bytes, observed_free_bytes=free, copied_bytes=row['copied_bytes']))
    receipt = dict(schema_version=1, document_type='mea_maxwell_local_unit', complete=True,
        plan_digest=plan['digest'], unit_meta=row['unit_meta'], files=inventory(output))
    receipt['digest'] = digest(receipt)
    write_json(output/'conversion-unit.json', receipt)
    verify_local(output, (plan['digest'], row))
    destination = LocalStore(row['publication_root'])
    # Publish every dependency before the unit receipt; aggregate completion is later.
    for relative in sorted(receipt['files']) + ['conversion-unit.json']:
        require(shutil.disk_usage(output.parent).free >= row['minimum_free_bytes'],
                'Materialization exhausted its free-space reserve')
        ref = destination.upload_file(row['publication_root'] + relative, output/relative)
        destination.verify_contents(ref)
    verify_local(parse_file(row['publication_root'], prefix=True), (plan['digest'], row))
    return receipt


def commit(plan, paths):
    checked_plan(plan)
    rows = {row['unit_meta']['unit_id']: row for row in plan['units']}
    require(len(paths) == len(rows), 'Bridge commit requires every materialized unit once')
    observed, units = set(), []
    for path in paths:
        local = verify_local(path)
        unit_id = local['unit_meta']['unit_id']
        require(unit_id in rows and unit_id not in observed, 'Conflicting materialized units')
        observed.add(unit_id)
        row = rows[unit_id]
        require(local['plan_digest'] == plan['digest'] and local['unit_meta'] == row['unit_meta'],
                'Materialized unit differs from its bridge invocation')
        published = parse_file(row['publication_root'], prefix=True)
        require(verify_local(published, (plan['digest'], row)) == local,
                'Published unit differs from its verified materialization')
        units.append(dict(metadata=local['unit_meta'], input=str(published),
            receipt=LocalStore().stat((published/'conversion-unit.json').as_uri())))
    result = dict(schema_version=1, document_type='mea_maxwell_bridge_completion', complete=True,
        plan_digest=plan['digest'], native_artifacts=plan['native_artifacts'],
        wrapper_owner=plan['wrapper']['owner'], units=sorted(units, key=lambda u: u['metadata']['unit_id']))
    root = plan['wrapper']['publication_root']
    store = LocalStore(root)
    units_ref = store.put_json(root + 'units.json', [dict(metadata=u['metadata'], input=u['input']) for u in result['units']])
    result['units_file'] = units_ref
    published = store.put_json(root + 'completion.json', result)
    store.read_json(published['uri'], expected=published)
    return dict(result, completion=published)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    pre = commands.add_parser('prepare'); pre.add_argument('--request', required=True); pre.add_argument('--artifacts', required=True)
    unit = commands.add_parser('materialize'); unit.add_argument('--plan', required=True); unit.add_argument('--unit-id', required=True); unit.add_argument('--requested-bytes', type=int, required=True)
    end = commands.add_parser('commit'); end.add_argument('--plan', required=True); end.add_argument('--units', required=True)
    check = commands.add_parser('verify'); check.add_argument('directory')
    args = parser.parse_args()
    if args.operation == 'prepare':
        write_json('bridge-plan.json', prepare(args.artifacts, read_json(args.request)))
    elif args.operation == 'materialize':
        materialize(read_json(args.plan), args.unit_id, 'unit', args.requested_bytes)
    elif args.operation == 'commit':
        value = commit(read_json(args.plan), [p.resolve() for p in sorted(Path(args.units).glob('unit*'))])
        write_json('bridge-result.json', value)
    else:
        value = verify_local(Path(args.directory))
        print(json.dumps(dict(verified=True, unit_id=value['unit_meta']['unit_id'])))


if __name__ == '__main__':
    try:
        main()
    except (ContractError, S3Error, ValueError, OSError, KeyError, TypeError) as error:
        print('[maxwell_bridge] ' + str(error), file=sys.stderr)
        raise SystemExit(2) from None
