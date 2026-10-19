#!/usr/bin/env python3
"""Check frozen bridge claims on real units; run with the pinned native runtime.

This component/CLI matrix accompanies real Nextflow process-boundary controls.
It does not replace the full native/sibling/source-protection acceptance matrix.
"""
import argparse
from copy import deepcopy
import gc
import hashlib
import json
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / 'pipeline/helpers'))
import maxwell_bridge as bridge


def inventory(root):
    return {str(p.relative_to(root)): dict(size=p.stat().st_size,
            sha256=hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob('*') if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unit', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--bindings', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    plan = bridge.read_json(args.plan)
    original = bridge.read_json(args.unit / 'conversion-unit.json')
    row, = [u for u in plan['units'] if u['unit_meta']['unit_id'] == original['unit_meta']['unit_id']]
    submitted, = [u['metadata'] for u in bridge.read_json(args.bindings)
                  if u['metadata']['unit_id'] == original['unit_meta']['unit_id']]
    assert bridge.canonical_json({k:v for k,v in original['unit_meta'].items()
                                  if k != 'maxwell_conversion'}) == bridge.canonical_json(submitted)
    expected = (plan['digest'], row)
    before = inventory(args.unit)
    results = []
    for claim in [None, expected]:
        actual = bridge.verify_local(args.unit, claim)
        assert bridge.canonical_json(actual) == bridge.canonical_json(original)
    results.append(dict(case='unchanged-independent-and-expected', accepted=True))

    def case(name, path, value, message, remove=False):
        root = args.output / name
        import shutil
        shutil.copytree(args.unit, root)
        changed = deepcopy(original)
        target = changed
        for key in path[:-1]:
            target = target[key]
        if remove:
            target.pop(path[-1])
        else:
            target[path[-1]] = value
        changed.pop('digest')
        changed['digest'] = bridge.digest(changed)
        assert bridge.canonical_json(changed) != bridge.canonical_json(original), name
        (root / 'conversion-unit.json').write_bytes(bridge.canonical_json(changed))
        try:
            bridge.verify_local(root, expected)
        except bridge.ContractError as error:
            assert str(error) == message, (name, str(error))
        else:
            raise AssertionError(name + ' accepted')
        assert {k:v for k,v in inventory(root).items() if k != 'conversion-unit.json'} == {
            k:v for k,v in before.items() if k != 'conversion-unit.json'}
        results.append(dict(case=name, target=path, value=value, remove=remove,
                            rejected=True, intended_error=message))
        gc.collect()
        return root

    for name, value, remove in [('boolean', True, False), ('false', False, False),
                               ('float', 1.0, False), ('string', '1', False),
                               ('unsupported', 2, False), ('missing', None, True)]:
        case('receipt-schema-' + name, ['schema_version'], value,
             'Unsupported local conversion receipt', remove)
    for name, value, remove in [('boolean', True, False), ('float', 1.0, False),
                               ('unsupported', 2, False), ('missing', None, True)]:
        case('metadata-schema-' + name, ['unit_meta','maxwell_conversion','schema_version'], value,
             'Unsupported local conversion metadata', remove)
    frozen_error = 'Local unit differs from its frozen bridge plan'
    meta = original['unit_meta']['unknown_typed']
    assert type(meta['integer']) is int and float(meta['integer']) == meta['integer']
    float_root = case('integer-to-float', ['unit_meta','unknown_typed','integer'], float(meta['integer']), frozen_error)
    case('integer-to-boolean', ['unit_meta','unknown_typed','integer'], True, frozen_error)
    assert meta['signed_zero'].hex() == '-0x0.0p+0'
    case('signed-zero', ['unit_meta','unknown_typed','signed_zero'], 0.0, frozen_error)
    case('large-integer', ['unit_meta','unknown_typed','large_integer'], meta['large_integer'] + 1, frozen_error)
    assert meta['null'] is None
    case('nullable-loss', ['unit_meta','unknown_typed','null'], None, frozen_error, True)
    # Independent verification has no authority for this unknown caller field.
    independent = bridge.verify_local(float_root)
    assert type(independent['unit_meta']['unknown_typed']['integer']) is float
    results.append(dict(case='independent-unknown-metadata-authority', accepted=True))

    details = original['unit_meta']['maxwell_conversion']
    base = ['unit_meta','maxwell_conversion']
    case('inventory-size-float', ['files','output.nwb','size'], float(before['output.nwb']['size']),
         'Local conversion dependency inventory changed')
    case('sibling-reference-size-float', base + ['artifacts','nwb','size'], float(details['artifacts']['nwb']['size']),
         'Local sibling differs from its native published object')
    case('provenance-reference-size-float', base + ['native_artifacts','size'], float(details['native_artifacts']['size']),
         'Local provenance differs from its native published object')
    case('evidence-reference-size-float', base + ['native_evidence','size'], float(details['native_evidence']['size']),
         'Local conversion lost its complete native outcome')
    case('recording-index-float', base + ['native_unit','metadata','recording_index'],
         float(details['native_unit']['metadata']['recording_index']), 'Local unit lost its explicit native binding')
    companion = details['settings']['effective']['create_linked_maxwell_companion']
    assert type(companion) is bool
    case('companion-setting-boolean-to-integer', base + ['settings','effective','create_linked_maxwell_companion'],
         int(companion), 'Local native ownership or settings changed')

    for name, value, remove in [('boolean', True, False), ('float', 1.0, False),
                               ('string', '1', False), ('unsupported', 2, False), ('missing', None, True)]:
        bad = deepcopy(plan)
        if remove:
            bad.pop('schema_version')
        else:
            bad['schema_version'] = value
        bad.pop('digest'); bad['digest'] = bridge.digest(bad)
        path = args.output / ('plan-schema-' + name + '.json')
        path.write_bytes(bridge.canonical_json(bad))
        for operation in ['materialize', 'commit']:
            try:
                if operation == 'materialize':
                    bridge.materialize(bad, row['unit_meta']['unit_id'], args.output / ('no-unit-' + name), row['requested_bytes'])
                else:
                    bridge.commit(bad, [])
            except bridge.ContractError as error:
                assert str(error) == 'Unsupported conversion bridge plan', (name, operation, str(error))
            else:
                raise AssertionError(name + ' ' + operation + ' accepted')
        assert not (args.output / ('no-unit-' + name)).exists()
        results.append(dict(case='plan-schema-' + name, both_operations_reject_before_materialization=True))
    report = bridge.read_json(args.unit / 'conversion/native-evidence.json')
    assert bridge.completed_phases(report, details['settings'])
    for value in [False, 0.0, '0', None]:
        changed = deepcopy(report); changed['phases'][0]['exit_code'] = value
        assert not bridge.completed_phases(changed, details['settings'])
        results.append(dict(case='phase-status-type', value=value, scope='shared phase predicate; native publication control is separate'))

    for label, directory, exit_code in [('valid', args.unit, 0), ('bad-format', args.output/'receipt-schema-boolean', 2),
                                         ('independent-metadata', float_root, 0)]:
        command = [sys.executable, str(SOURCE/'pipeline/helpers/maxwell_bridge.py'), 'verify', str(directory)]
        process = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (args.output/(label+'.stdout')).write_text(process.stdout)
        (args.output/(label+'.stderr')).write_text(process.stderr)
        assert process.returncode == exit_code, (label, process.returncode, process.stderr)
        if exit_code:
            assert '[maxwell_bridge] Unsupported local conversion receipt' in process.stderr
        results.append(dict(case='CLI-'+label, command=command, actual_exit=process.returncode))
    assert inventory(args.unit) == before
    with (args.output/'result.json').open('x') as f:
        f.write(json.dumps(dict(passed=True, cases=results, original_unit_unchanged=True,
            unknown_metadata_independent_authority_preserved=True,
            scope='Real unit component/CLI; real Nextflow and full native matrices remain required'), indent=2)+'\n')
    print(json.dumps(dict(passed=True, cases=len(results), original_unit_unchanged=True)), flush=True)


if __name__ == '__main__':
    main()
