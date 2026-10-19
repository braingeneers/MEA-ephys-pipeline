#!/usr/bin/env python3
"""Check original-source and independent-proof bindings with private counterexamples.

Resealed outer manifests below are explicitly synthetic negative-test evidence.
Original scientific producer receipts, immutable seals and input truth stay intact.
"""
from pathlib import Path
import argparse
import json
import resource
import shutil
import stat
import subprocess
import sys
import time
import traceback

import h5py

from original_nwb import FrozenOriginalScope, OriginalCanonicalCheck
from canonical import CanonicalCheck, expected_graph
from inputs import sha


def reseal(root, relative):
    path = root / relative
    freeze = json.loads((root / 'freeze.json').read_text())
    freeze['files'][relative] = dict(bytes=path.stat().st_size, sha256=sha(path), mode=stat.S_IMODE(path.stat().st_mode))
    freeze['total_file_bytes'] = sum(row['bytes'] for row in freeze['files'].values())
    freeze['synthetic_counterexample_manifest'] = True
    (root / 'freeze.json').write_text(json.dumps(freeze, indent=2) + '\n')
    return sha(root / 'freeze.json')


def prepare(original, proof, output):
    output.mkdir(parents=True, exist_ok=False)
    original_digest, proof_digest = sha(original / 'freeze.json'), sha(proof / 'freeze.json')
    scope = FrozenOriginalScope(original, original_digest, proof, proof_digest)
    folder, facts, receipt, nwb, binding = scope.bind('enabled', 'maxone')
    definitions = [
        ('source_same_uri', 'Independent source/projection bytes'),
        ('projection_same_uri', 'Independent source/projection bytes'),
        ('facts_numeric_type', 'NWB proof facts/receipt binding'),
        ('receipt_routing', 'NWB proof facts/receipt binding'),
        ('receipt_source_uri', 'NWB proof facts/receipt binding'),
        ('unreviewed_nwb', 'NWB has no complete independent alias proof'),
        ('alias_path_scope', 'Complete proven path/address mapping'),
        ('alias_address_scope', 'Complete proven path/address mapping'),
        ('alias_report_claim', 'Complete unchanged baseline alias proof'),
        ('proof_implementation', 'Independent baseline proof implementation'),
        ('producer_image', 'Actual original producer source/image'),
        ('wrong_original_digest', 'Trusted original freeze identity'),
        ('wrong_proof_digest', 'Trusted baseline-proof freeze identity'),
    ]
    rows = []
    for name, expected in definitions:
        case = output / name
        case.mkdir()
        a, b = case / 'original', case / 'proof'
        shutil.copytree(original, a)
        shutil.copytree(proof, b)
        original_sha, proof_sha = original_digest, proof_digest
        case_name = 'maxtwo_routed' if name == 'receipt_routing' else 'maxone'
        relative, owner = None, None
        if name in ('source_same_uri', 'unreviewed_nwb'):
            relative = 'inputs/maxone/recording.raw.h5' if name == 'source_same_uri' else 'published/enabled/maxone/output.nwb'
            dataset = '/data_store/data0000/groups/routed/raw' if name == 'source_same_uri' else '/acquisition/ElectricalSeries/data'
            with h5py.File(a / relative, 'r+') as handle:
                handle[dataset][0, 0] = int(handle[dataset][0, 0]) + 1
            owner = a
        elif name in ('projection_same_uri', 'facts_numeric_type', 'receipt_routing', 'receipt_source_uri', 'producer_image'):
            relative = {'projection_same_uri':'inputs/maxone/projection.json', 'facts_numeric_type':'inputs/maxone/facts.json',
                        'receipt_routing':'receipts/enabled/maxtwo_routed.json', 'receipt_source_uri':'receipts/enabled/maxone.json',
                        'producer_image':'identity/original-runtime.json'}[name]
            value = json.loads((a / relative).read_text())
            if name == 'projection_same_uri': value['common']['notes'] = 'changed under the same URI'
            elif name == 'facts_numeric_type': value['schema_version'] = 2.0
            elif name == 'receipt_routing': value['selected_raw_dataset'] = '/data_store/data0001/groups/routed/raw'
            elif name == 'receipt_source_uri': value['source_uri'] = 'file:///inputs/other/recording.raw.h5'
            else: value['image'] = 'unreviewed-image'
            (a / relative).write_text(json.dumps(value, indent=2) + '\n')
            owner = a
        elif name in ('alias_path_scope', 'alias_address_scope'):
            relative = 'reports/comparison-scope.json'
            value = json.loads((b / relative).read_text())
            target = value['allowed_files'][sha(nwb)]
            if name == 'alias_path_scope':
                old = next(iter(target['comparison_path_mapping']))
                target['comparison_path_mapping'][old] = '/general/devices/DeviceEcephys'
            else:
                target['distinct_physical_equivalence_pairs'][0][1] += 1
            (b / relative).write_text(json.dumps(value, indent=2) + '\n')
            owner = b
        elif name == 'alias_report_claim':
            relative = 'final-positives/reports/maxone-enabled-final.json'
            value = json.loads((b / relative).read_text())
            value['passed'] = False
            (b / relative).write_text(json.dumps(value, indent=2) + '\n')
            owner = b
        elif name == 'proof_implementation':
            relative = 'code-final/baseline_aliases.py'
            with (b / relative).open('a') as stream: stream.write('\n# changed proof identity\n')
            owner = b
        elif name == 'wrong_original_digest': original_sha = '0' * 64
        else: proof_sha = '0' * 64
        if owner is not None:
            original_sha = reseal(a, relative) if owner == a else original_sha
            proof_sha = reseal(b, relative) if owner == b else proof_sha
            old_root = original if owner == a else proof
            assert sha(owner / relative) != sha(old_root / relative), name
        rows.append(dict(name=name, expected_error=expected, case=case_name,
                         original_freeze_sha256=original_sha, proof_freeze_sha256=proof_sha,
                         changed_file=relative, changed_sha256=sha(owner / relative) if owner is not None else None,
                         synthetic_outer_manifest=owner is not None, synthetic_producer_evidence=True))
    assert sha(original / 'freeze.json') == original_digest and sha(proof / 'freeze.json') == proof_digest
    (output / 'controls.json').write_text(json.dumps(dict(controls=rows, original_freeze_sha256=original_digest,
        proof_freeze_sha256=proof_digest, original_immutable_evidence_modified=False,
        changed_file_controls=sum(r['changed_file'] is not None for r in rows), changed_argument_controls=2), indent=2) + '\n')
    print(json.dumps(dict(prepared=len(rows), changed_file_controls=11, changed_argument_controls=2)))


def one(root, index, report):
    row = json.loads((root / 'controls.json').read_text())['controls'][index]
    case = root / row['name']
    start = time.monotonic()
    try:
        scope = FrozenOriginalScope(case / 'original', row['original_freeze_sha256'], case / 'proof', row['proof_freeze_sha256'])
        scope.bind('enabled', row['case'])
    except ValueError as error:
        assert row['expected_error'] in str(error), (row['name'], str(error))
        result = dict(name=row['name'], passed=True, intended_error=str(error), exception_type=type(error).__name__,
                      frames=traceback.format_exc(), synthetic_counterexample=True, changed_file=row['changed_file'],
                      changed_sha256=row['changed_sha256'], duration_s=time.monotonic()-start,
                      peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    else:
        raise AssertionError('Original scope counterexample accepted: ' + row['name'])
    with report.open('x') as stream: stream.write(json.dumps(result, indent=2) + '\n')


def check(root, reports, original, proof):
    reports.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((root / 'controls.json').read_text())
    scope = FrozenOriginalScope(original, manifest['original_freeze_sha256'], proof, manifest['proof_freeze_sha256'])
    folder, facts, receipt, nwb, binding = scope.bind('enabled', 'maxone')
    nodes, _, _ = expected_graph(folder, facts, receipt)
    oracle = OriginalCanonicalCheck(nodes, binding)
    copy = reports / 'foreign-identical.nwb'
    shutil.copy2(nwb, copy)
    alias, canonical = '/~obj_pointed_by_10328', '/general/extracellular_ephys/electrodes'
    with h5py.File(nwb, 'r') as left, h5py.File(copy, 'r') as foreign:
        assert oracle.physical_matches(left[alias], left[canonical])
        assert not oracle.physical_matches(left[alias], foreign[canonical])
        assert not CanonicalCheck(nodes).physical_matches(left[alias], left[canonical])
        assert oracle.physical_path('/~unknown') == '/~unknown'
    results = []
    for i, row in enumerate(manifest['controls']):
        dest = reports / (row['name'] + '.json')
        command = [sys.executable, __file__, 'one', '--controls', str(root), '--index', str(i), '--report', str(dest)]
        (reports / (row['name'] + '-command.json')).write_text(json.dumps(command, indent=2) + '\n')
        with (reports / (row['name'] + '.log')).open('w') as stream:
            process = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=90)
        (reports / (row['name'] + '-exit.json')).write_text(json.dumps(dict(exit=process.returncode)) + '\n')
        assert process.returncode == 0, (row['name'], process.returncode)
        results.append(json.loads(dest.read_text()))
        print(row['name'], 'intended binding rejection', flush=True)
    assert sha(original / 'freeze.json') == manifest['original_freeze_sha256'] and sha(proof / 'freeze.json') == manifest['proof_freeze_sha256']
    with (reports / 'summary.json').open('x') as stream:
        stream.write(json.dumps(dict(passed=True, controls=results, changed_file_controls=11, changed_argument_controls=2,
            explicit_outer_resealing_test_only=True, actual_same_file_equivalence_checked=True,
            foreign_file_same_address_rejected=True, native_default_stays_strict=True, unknown_paths_remain_unmapped=True,
            original_immutable_evidence_modified=False), indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    prep = sub.add_parser('prepare')
    run = sub.add_parser('check')
    for command in (prep, run):
        command.add_argument('--original', required=True, type=Path)
        command.add_argument('--proof', required=True, type=Path)
    prep.add_argument('--output', required=True, type=Path)
    run.add_argument('--controls', required=True, type=Path)
    run.add_argument('--reports', required=True, type=Path)
    worker = sub.add_parser('one')
    worker.add_argument('--controls', required=True, type=Path)
    worker.add_argument('--index', required=True, type=int)
    worker.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, '/opt/ephys-modular/maxwell/scripts')
    from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
    configure_bundled_maxwell_hdf5_plugin()
    if args.operation == 'prepare': prepare(args.original, args.proof, args.output)
    elif args.operation == 'check': check(args.controls, args.reports, args.original, args.proof)
    else: one(args.controls, args.index, args.report)
