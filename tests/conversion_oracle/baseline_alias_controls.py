#!/usr/bin/env python3
"""Retain real, schema-valid counterexamples to baseline alias equivalence."""
from pathlib import Path
import argparse
import json
import resource
import shutil
import subprocess
import sys
import time
import traceback

import h5py
import numpy as np
from pynwb import validate

from baseline_aliases import address, prove_file, sha


def configure():
    sys.path.insert(0, '/opt/ephys-modular/maxwell/scripts')
    from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
    configure_bundled_maxwell_hdf5_plugin()


def prepare(original, output):
    output.mkdir(parents=True, exist_ok=False)
    source = original / 'published/enabled/maxone/output.nwb'
    report = prove_file(source, original / 'inputs/maxone', original / 'receipts/enabled/maxone.json')
    aliases = {v['authored_key']: k for k, v in report['complete_alias_proofs'].items()}
    table, group = aliases['electrodes'], aliases['group']
    anchors = report['canonical_anchors']
    a, b = [anchors['cultures/parent-' + k]['path'] for k in ('a', 'b')]
    parents = anchors['cultures/recorded/@parent_cultures']['path']
    definitions = [
        ('alias_numeric_bit', '/rel_x:payload'),
        ('alias_signed_zero', '/rel_x:payload'),
        ('alias_string_payload', '/channel_name:payload'),
        ('alias_reference_target', ': ordered reference target: complete attributes'),
        ('alias_attribute_type', ': complete attributes/description/native_type'),
        ('alias_uuid', ': complete attributes/object_id'),
        ('alias_missing_column', ': complete children'),
        ('alias_numeric_dtype', '/rel_y:native_type'),
        ('same_id_parent_ages', 'cultures/parent-a.age: exact scalar value/type'),
        ('parent_reference_order', 'cultures/recorded/@parent_cultures.data[0]: canonical object identity'),
        ('parent_reference_duplicate', 'cultures/recorded/@parent_cultures.data[1]: canonical object identity'),
        ('parent_reference_cardinality', 'cultures/recorded/@parent_cultures.data: reference shape/cardinality'),
        ('unmatched_expanded_root', ': no unique complete authored equivalent'),
        ('rebound_hard_alias', 'Expanded root address marker'),
    ]
    controls = []
    for name, expected in definitions:
        path = output / (name + '.nwb')
        shutil.copy2(source, path)
        with h5py.File(path, 'r+') as h:
            if name == 'alias_numeric_bit':
                data = h[table + '/rel_x']
                data[1] = np.nextafter(data[1], np.inf)
            elif name == 'alias_signed_zero':
                data = h[table + '/rel_x']
                assert float(data[0]).hex() == '0x0.0p+0'
                data[0] = -0.0
            elif name == 'alias_string_payload':
                h[table + '/channel_name'][0] = 'changed'
            elif name == 'alias_reference_target':
                h.copy(anchors['group']['path'], h['/'], name='temporary_group')
                obj = h['temporary_group']
                obj.attrs.modify('location', 'different')
                target = '~obj_pointed_by_' + str(address(obj))
                h[target] = obj
                del h['temporary_group']
                h[table + '/group'][0] = obj.ref
            elif name == 'alias_attribute_type':
                old = h[group].attrs['description']
                del h[group].attrs['description']
                h[group].attrs.create('description', str(old).encode(), dtype='S' + str(len(old)))
            elif name == 'alias_uuid':
                h[group].attrs.modify('object_id', anchors['device']['measured_uuid'])
            elif name == 'alias_missing_column':
                del h[table + '/rel_y']
            elif name == 'alias_numeric_dtype':
                source_data = h[table + '/rel_y']
                values, attrs = source_data[:], dict(source_data.attrs)
                chunks, compression, opts = source_data.chunks, source_data.compression, source_data.compression_opts
                del h[table + '/rel_y']
                replacement = h[table].create_dataset('rel_y', data=values.astype('<f4'), chunks=chunks,
                                                      compression=compression, compression_opts=opts)
                for key, value in attrs.items():
                    replacement.attrs[key] = value
            elif name == 'same_id_parent_ages':
                assert h[a].attrs['culture_id'] == h[b].attrs['culture_id'] == 'shared-parent'
                first, second = h[a].attrs['age'], h[b].attrs['age']
                assert first != second
                h[a].attrs.modify('age', second)
                h[b].attrs.modify('age', first)
            elif name == 'parent_reference_order':
                data = h[parents]
                data[:] = data[:][::-1]
            elif name == 'parent_reference_duplicate':
                h[parents][1] = h[parents][0]
            elif name == 'parent_reference_cardinality':
                old = h[parents]
                attrs, target = dict(old.attrs), old[0]
                del h[parents]
                owner, field = parents.rsplit('/', 1)
                replacement = h[owner].create_dataset(field, shape=(1,), dtype=h5py.ref_dtype)
                replacement[0] = target
                for key, value in attrs.items():
                    replacement.attrs[key] = value
            elif name == 'unmatched_expanded_root':
                obj = h.create_group('temporary_group')
                target = '~obj_pointed_by_' + str(address(obj))
                h[target] = obj
                del h['temporary_group']
            else:
                old = aliases['lines/source-line']
                del h[old]
                h[old] = h[anchors['lines/derived-line']['path']]
        changed = sha(path)
        assert changed != report['nwb_sha256']
        controls.append(dict(name=name, file=path.name, expected=expected, sha256=changed))
    assert sha(source) == report['nwb_sha256']
    manifest = dict(schema_version=1, synthetic_controls=True, source_sha256=report['nwb_sha256'],
                    source_file=str(source), facts=str(original / 'inputs/maxone'),
                    receipt=str(original / 'receipts/enabled/maxone.json'), controls=controls,
                    original_producer_receipts_modified=False, original_publication_acceptance_claimed=False)
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(dict(prepared=True, real_control_types=len(controls), original_unchanged=True)))


def check_one(controls, index, report):
    manifest = json.loads((controls / 'manifest.json').read_text())
    row = manifest['controls'][index]
    path = controls / row['file']
    assert sha(path) == row['sha256'] != manifest['source_sha256']
    schema = [str(x) for x in validate(path=str(path))]
    assert not schema, schema
    started = time.monotonic()
    try:
        prove_file(path, Path(manifest['facts']), Path(manifest['receipt']))
    except ValueError as error:
        frames = traceback.format_exc()
        assert row['expected'] in str(error), (row['name'], row['expected'], str(error))
        assert 'baseline_aliases.py' in frames
        result = dict(name=row['name'], passed=True, rejected=True, intended_error=str(error),
                      exception_type=type(error).__name__, exception_frames=frames, schema_errors=schema,
                      changed_sha256=sha(path), duration_s=time.monotonic() - started,
                      peak_process_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      synthetic_control=True, source_sha256=manifest['source_sha256'])
    else:
        raise AssertionError('Changed baseline accepted: ' + row['name'])
    assert sha(path) == row['sha256']
    with report.open('x') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps(dict(name=row['name'], passed=True, intended_error=result['intended_error'])))


def check(controls, reports):
    reports.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((controls / 'manifest.json').read_text())
    # An unchanged full positive precedes the fresh per-control processes.
    positive = prove_file(Path(manifest['source_file']), Path(manifest['facts']), Path(manifest['receipt']))
    rows = []
    for i, row in enumerate(manifest['controls']):
        target = reports / (row['name'] + '.json')
        command = [sys.executable, __file__, 'check-one', '--controls', str(controls), '--index', str(i), '--report', str(target)]
        (reports / (row['name'] + '-command.json')).write_text(json.dumps(command, indent=2) + '\n')
        with (reports / (row['name'] + '.log')).open('w') as stream:
            process = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=90)
        (reports / (row['name'] + '-exit.json')).write_text(json.dumps(dict(exit=process.returncode)) + '\n')
        assert process.returncode == 0, (row['name'], process.returncode)
        rows.append(json.loads(target.read_text()))
        print(row['name'], 'intended rejection', flush=True)
    assert sha(Path(manifest['source_file'])) == manifest['source_sha256'] == positive['nwb_sha256']
    result = dict(passed=True, unchanged_complete_positive=True, real_control_types=len(rows), controls=rows,
                  source_unchanged=True, controls_unchanged=True, all_schema_valid=True,
                  no_timeout_oom_or_unrelated_error_accepted=True, complete_artifact_acceptance=False)
    with (reports / 'summary.json').open('x') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--original', required=True, type=Path)
    prep.add_argument('--output', required=True, type=Path)
    run = sub.add_parser('check')
    run.add_argument('--controls', required=True, type=Path)
    run.add_argument('--reports', required=True, type=Path)
    one = sub.add_parser('check-one')
    one.add_argument('--controls', required=True, type=Path)
    one.add_argument('--index', required=True, type=int)
    one.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    configure()
    if args.operation == 'prepare':
        prepare(args.original, args.output)
    elif args.operation == 'check':
        check(args.controls, args.reports)
    else:
        check_one(args.controls, args.index, args.report)
