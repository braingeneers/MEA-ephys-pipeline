#!/usr/bin/env python3
"""Exercise early caller metadata validation on real immutable stage bundles.

Run in the pinned base scientific image. These component/CLI checks accompany
real Nextflow rejection/positive runs; they do not replace that boundary evidence.
"""
import argparse
from copy import deepcopy
import gc
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / 'pipeline/helpers'))
import portable_bundle as portable
import stage_bundle as stages


def inventory(root):
    return {str(p.relative_to(root)): dict(bytes=p.stat().st_size, sha256=portable.sha256(p))
            for p in root.rglob('*') if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--positive', type=Path, required=True)
    parser.add_argument('--zero', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    outcomes_path = SOURCE / 'capsules/kilosort4/code/outcomes.py'
    outcomes = stages.classifier(outcomes_path)
    records = {name: stages.read_record(root) for name, root in
               [('positive', args.positive), ('zero', args.zero)]}
    before = {name: inventory(root) for name, root in
              [('positive', args.positive), ('zero', args.zero)]}
    results = []
    for name, root in [('positive', args.positive), ('zero', args.zero)]:
        record = records[name]
        meta = record['metadata']['unit_meta']
        for mode in ['contents_only', 'unchanged', 'extension']:
            incoming = None if mode == 'contents_only' else deepcopy(meta)
            if mode == 'extension':
                incoming['new_invocation_note'] = {'text': "literal $() `ticks` 'quote'\nnext line", 'enabled': True}
            actual = stages.verify(root, record['stage'], record['unit_id'], outcomes,
                                   unit_meta=incoming)
            assert portable.encoded(actual) == portable.encoded(record)
            results.append(dict(case=name + '-' + mode, accepted=True))
            gc.collect()
    positive = records['positive']
    original = positive['metadata']['unit_meta']
    cases = {}
    def variant(name, change):
        value = deepcopy(original)
        change(value)
        assert portable.encoded(value) != portable.encoded(original), name
        cases[name] = value
    variant('projection_reference', lambda m: m['maxwell_conversion']['native_unit']['projection'].update(
        sha256='0' * 64, etag='sha256-' + '0' * 64))
    variant('recording_reference', lambda m: m['maxwell_conversion']['native_unit'].update(recording_id='other-recording'))
    variant('session_identity', lambda m: m['maxwell_conversion']['settings']['effective'].update(session_identifier_prefix='other-session'))
    assert original['unknown_typed']['null'] is None
    variant('nullable_loss', lambda m: m['unknown_typed'].pop('null'))
    variant('integer_to_bool', lambda m: m['unknown_typed'].update(integer=True))
    variant('integer_to_float', lambda m: m['unknown_typed'].update(integer=float(m['unknown_typed']['integer'])))
    assert original['unknown_typed']['signed_zero'].hex() == '-0x0.0p+0'
    variant('signed_zero', lambda m: m['unknown_typed'].update(signed_zero=0.0))
    variant('large_integer', lambda m: m['unknown_typed'].update(large_integer=m['unknown_typed']['large_integer'] + 1))
    for name, changed in cases.items():
        with patch.object(stages, 'scientific_policy', side_effect=AssertionError('Scientific policy ran before metadata rejection')) as policy:
            try:
                stages.verify(args.positive, positive['stage'], positive['unit_id'], outcomes,
                              unit_meta=changed)
            except portable.BundleError as error:
                assert str(error) == 'Stage invocation dropped or changed predecessor unit metadata', (name, error)
            else:
                raise AssertionError(name + ' accepted')
            assert policy.call_count == 0
        results.append(dict(case=name, rejected_before_scientific_policy=True))
        gc.collect()
    for label, meta in [('unchanged', original), ('wrong_projection', cases['projection_reference'])]:
        path = args.output / (label + '.json')
        path.write_bytes(portable.encoded(meta))
        command = [sys.executable, str(SOURCE / 'pipeline/helpers/stage_bundle.py'),
                   '--outcomes-code', str(outcomes_path), 'verify', '--bundle', str(args.positive),
                   '--kind', positive['stage'], '--unit-id', positive['unit_id'], '--unit-meta', str(path)]
        process = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (args.output / (label + '.stdout')).write_text(process.stdout)
        (args.output / (label + '.stderr')).write_text(process.stderr)
        assert process.returncode == (0 if label == 'unchanged' else 1)
        if label == 'wrong_projection':
            assert 'Stage invocation dropped or changed predecessor unit metadata' in process.stderr
        results.append(dict(case='CLI-' + label, command=command, actual_exit=process.returncode))
    for name, value in [('null', None), ('list', []), ('bool', True)]:
        path = args.output / (name + '.json'); path.write_text(json.dumps(value))
        command = [sys.executable, str(SOURCE / 'pipeline/helpers/stage_bundle.py'),
                   '--outcomes-code', str(outcomes_path), 'verify', '--bundle', str(args.positive),
                   '--kind', positive['stage'], '--unit-id', positive['unit_id'], '--unit-meta', str(path)]
        process = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.returncode == 1 and 'Stage invocation unit metadata must be an object' in process.stderr
        results.append(dict(case='CLI-object-required-' + name, actual_exit=process.returncode))
    request = dict(schema_version=1, phase='exported', unit_id=positive['unit_id'],
                   unit_meta=cases['projection_reference'], context=positive['metadata']['context'],
                   previous=0, input_count=1, pieces=[])
    with patch.object(stages, 'scientific_policy', side_effect=AssertionError('Build reached scientific policy')) as policy:
        try:
            stages.build(request, [args.positive], args.output / 'invalid-build', outcomes)
        except portable.BundleError as error:
            assert str(error) == 'Stage invocation dropped or changed predecessor unit metadata'
        else:
            raise AssertionError('Build accepted changed metadata')
        assert policy.call_count == 0 and not (args.output / 'invalid-build').exists()
    for name, root in [('positive', args.positive), ('zero', args.zero)]:
        assert inventory(root) == before[name]
    report = dict(passed=True,scope='component and CLI on real bundles; real Nextflow validation required separately',
                  cases=results, build_uses_same_early_guard=True, originals_unchanged=True,
                  genuine_positive_units=sum(v['sorted_units'] for v in positive['metadata']['outcomes'].values()),
                  genuine_zero_units=sum(v['sorted_units'] for v in records['zero']['metadata']['outcomes'].values()))
    with (args.output / 'result.json').open('x') as stream:
        stream.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps(dict(passed=True,cases=len(results),build_uses_same_early_guard=True,originals_unchanged=True)),flush=True)


if __name__ == '__main__':
    main()
