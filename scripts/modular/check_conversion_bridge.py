#!/usr/bin/env python3
"""Exercise conversion bridge guards against a real completed native fixture.

Run in the pinned converter image, passing its runtime/common on PYTHONPATH.
The supplied real bridge plan/request and all native files remain read-only;
corruptions affect new copies under --output. These are adapter checks, not full
converter/sorter parity. Scientific generation is a separate Nextflow fixture.
"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'pipeline/helpers'))
import maxwell_bridge as bridge


def json_identity(value):
    """JSON equality with integer/float distinctions and IEEE signed zero."""
    if type(value) is dict:
        return ('object', {key: json_identity(item) for key, item in value.items()})
    if type(value) is list:
        return ('array', [json_identity(item) for item in value])
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError('Non-finite input oracle')
        return ('float', value.hex())
    return (type(value).__name__, value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--bindings', type=Path, required=True, help='Original submitted bindings JSON, never the generated request')
    parser.add_argument('--recordings', type=Path, required=True, help='Original submitted recordings JSON')
    parser.add_argument('--settings', type=Path, help='Original submitted settings JSON; omitted means {}')
    parser.add_argument('--owner-root', type=Path, default=Path(__file__).resolve().parents[2]/'pipeline/components/maxwell',
                        help='Reviewed exported owner; select the matching historical export explicitly for old fixtures')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    plan = bridge.read_json(args.plan)
    request = bridge.read_json(args.request)
    bindings = json.loads(args.bindings.read_text())
    recordings = json.loads(args.recordings.read_text())
    settings = json.loads(args.settings.read_text()) if args.settings else {}
    distribution = json.loads((args.owner_root/'export-manifest.json').read_text())
    image = json.loads((args.owner_root/'image.json').read_text())
    version = re.search(r"(?m)^\s*version\s*=\s*['\"]([^'\"]+)['\"]", (args.owner_root/'nextflow.config').read_text()).group(1)
    if version != distribution['owner_version'] or image != distribution['scientific_image']:
        raise ValueError('Reviewed owner manifest/config/image disagree')
    expected_metadata = {row['metadata']['unit_id']: row['metadata'] for row in bindings}
    if len(expected_metadata) != len(bindings):
        raise ValueError('Duplicate submitted unit identity')
    unit = plan['units'][0]
    original = bridge.parse_file(unit['publication_root'], prefix=True)
    controls = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for row in plan['units'] for p in bridge.parse_file(row['publication_root'], prefix=True).rglob('*') if p.is_file()}
    for row in plan['units']:
        for ref in list(row['files'].values()) + row['unit_meta']['maxwell_conversion']['native_unit']['sources']:
            p = bridge.parse_file(ref['uri']);controls[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    cases = []

    class BridgeTests(unittest.TestCase):
        def directory(self, label):
            location = args.output/(str(len(cases)).zfill(3)+'-'+label)
            location.mkdir()
            cases.append(str(location))
            return location

        def copied(self, label):
            root = self.directory(label)/'unit'
            shutil.copytree(original, root)
            return root

        def reseal(self, root):
            receipt = bridge.read_json(root/'conversion-unit.json')
            receipt['files'] = bridge.inventory(root)
            receipt['files'].pop('conversion-unit.json')
            receipt.pop('digest')
            receipt['digest'] = bridge.digest(receipt)
            (root/'conversion-unit.json').write_bytes(bridge.canonical_json(receipt))

        def test_complete_selection_owners_routing_and_numeric_types(self):
            self.assertEqual(len(plan['units']), 2)
            self.assertEqual(len({u['unit_meta']['unit_id'] for u in plan['units']}), 2)
            native_ids = set()
            for row in plan['units']:
                meta = row['unit_meta']; details = meta['maxwell_conversion']
                self.assertEqual(details['native_owner']['workflow_version'], version)
                self.assertEqual(details['native_owner']['image'], image['pinned_reference'])
                self.assertEqual(request['native']['distribution'], {
                    'source_commit': distribution['source_commit'], 'content_sha256': distribution['content_sha256']})
                self.assertEqual(details['wrapper_owner'], request['wrapper']['owner'])
                self.assertEqual(json_identity(details['settings']['submitted']), json_identity(settings))
                self.assertEqual(json_identity({key:value for key,value in meta.items() if key != 'maxwell_conversion'}),
                                 json_identity(expected_metadata[meta['unit_id']]))
                native_ids.add(details['native_unit']['unit_id'])
                self.assertEqual(json_identity(row['evidence']['conversion/recordings.json']), json_identity(recordings))
                self.assertEqual(json_identity(request['recordings']), json_identity(recordings))
                self.assertEqual(json_identity(request['wrapper']['unit_bindings']), json_identity(bindings))
                expected_kinds = set(bridge.NAMES)
                if settings.get('create_linked_maxwell_companion') is False:
                    expected_kinds.remove('maxwell_companion')
                self.assertEqual(set(details['artifacts']), expected_kinds)
                bridge.verify_local(bridge.parse_file(row['publication_root'], prefix=True), (plan['digest'], row))
            self.assertEqual(len(native_ids), 2)
            self.assertEqual({Path(r['input_uri']).name for r in request['recordings']}, {'recording.raw.h5'})

        def test_read_only_owning_commit_replay_matches_plan(self):
            fresh = bridge.prepare(plan['native_artifacts'], request)
            self.assertEqual(fresh, plan)

        def test_native_publication_cannot_be_created_or_repaired(self):
            store = bridge.PublishedOnly(request['native']['context']['output_base_uri'])
            path = self.directory('no-native-repair')/'unpublished.json'
            with self.assertRaises((bridge.ContractError, bridge.S3Error)):
                store.put_json(path.as_uri(), {})
            self.assertFalse(path.exists())
            with self.assertRaises(bridge.ContractError):
                store.put_json(plan['native_artifacts']['uri'], {'fabricated':True})

        def test_invalid_bindings_and_replacement_requests(self):
            variants = []
            for key in ('replace_primary','finalize_source','source_finalization'):
                value = copy.deepcopy(request);value['settings'][key] = True;variants.append(value)
            value = copy.deepcopy(request);value['wrapper']['unit_bindings'] *= 2;variants.append(value)
            value = copy.deepcopy(request);value['wrapper']['unit_bindings'][0]['metadata']['maxwell_conversion'] = {};variants.append(value)
            value = copy.deepcopy(request);value['wrapper']['unit_bindings'][0]['metadata']['unit_id'] = '../escape';variants.append(value)
            value = copy.deepcopy(request);value['wrapper']['publication_root'] = 'file:///tmp/wrong/';variants.append(value)
            for value in variants:
                with self.subTest(value=variants.index(value)), self.assertRaises(bridge.ContractError):
                    bridge.prepare(plan['native_artifacts'], value)

        def test_changed_selection_projection_owner_or_settings(self):
            variants = []
            value = copy.deepcopy(request);value['recordings'][0]['unknown_typed']['integer'] += 1;variants.append(value)
            value = copy.deepcopy(request);next(iter(value['recordings'][0]['nwb_metadata'].values()))['sha256'] = '0'*64;variants.append(value)
            value = copy.deepcopy(request);value['native']['owner']['workflow_version'] = value['wrapper']['owner']['workflow_version'];variants.append(value)
            value = copy.deepcopy(request);value['settings']['nwb_chunk_channels'] = 2;variants.append(value)
            for i,value in enumerate(variants):
                with self.subTest(case=i), self.assertRaises(bridge.ContractError):
                    bridge.prepare(plan['native_artifacts'], value)

        def test_every_missing_or_changed_dependency_stays_hard(self):
            names = list(unit['files']) + list(unit['evidence'])
            for name in names:
                for kind in ('missing','changed'):
                    root = self.copied(kind+'-'+Path(name).name)
                    target = root/name
                    if kind == 'missing':target.rename(root.parent/'missing-dependency')
                    else:
                        with target.open('r+b') as stream:stream.seek(0);stream.write(b'corrupt')
                    with self.subTest(name=name,kind=kind), self.assertRaises((bridge.ContractError,ValueError)):
                        bridge.verify_local(root)

        def test_resealed_sibling_and_provenance_changes_stay_hard(self):
            for name in ('output.nwb.lindi.json','conversion/projection.json','conversion/native-evidence.json','conversion/recordings.json'):
                root = self.copied('resealed-'+Path(name).name)
                (root/name).write_bytes((root/name).read_bytes()+b' ')
                self.reseal(root)
                with self.subTest(name=name), self.assertRaises(bridge.ContractError):bridge.verify_local(root)

        def test_symlink_and_unexpected_dependencies_stay_hard(self):
            root = self.copied('symlink')
            path = root/'output.nwb';path.rename(root.parent/'outside.nwb');path.symlink_to(root.parent/'outside.nwb')
            with self.assertRaises(bridge.ContractError):bridge.verify_local(root)
            root = self.copied('extra');(root/'unexpected.txt').write_text('unexpected')
            with self.assertRaises(bridge.ContractError):bridge.verify_local(root)

        def test_bad_plan_unknown_units_and_scratch_cannot_materialize(self):
            out = self.directory('no-materialization')/'unit'
            value = copy.deepcopy(plan);value['digest'] = '0'*64
            with self.assertRaises(bridge.ContractError):bridge.materialize(value,'invalid',out,unit['requested_bytes'])
            with self.assertRaises(bridge.ContractError):bridge.materialize(plan,'unknown',out,unit['requested_bytes'])
            with self.assertRaises(bridge.ContractError):bridge.materialize(plan,unit['unit_meta']['unit_id'],out,unit['requested_bytes']-1)
            with patch.object(bridge.shutil,'disk_usage',return_value=type('Space',(),{'free':0})()):
                with self.assertRaises(bridge.ContractError):bridge.materialize(plan,unit['unit_meta']['unit_id'],out,unit['requested_bytes'])
            self.assertFalse(out.exists())

        def test_interrupted_identical_materialization_can_reconfirm_publication(self):
            receipts = []
            for label in ('reconfirm-one','reconfirm-two'):
                out = self.directory(label)/'unit'
                receipts.append(bridge.materialize(plan,unit['unit_meta']['unit_id'],out,unit['requested_bytes']))
            self.assertEqual(receipts[0],receipts[1])
            self.assertEqual(receipts[0],bridge.read_json(original/'conversion-unit.json'))

        def test_incomplete_duplicate_or_changed_units_cannot_commit(self):
            roots = [bridge.parse_file(u['publication_root'],prefix=True) for u in plan['units']]
            for values in (roots[:1],[roots[0],roots[0]]):
                with self.assertRaises(bridge.ContractError):bridge.commit(plan,values)
            root = self.copied('metadata-change')
            receipt = bridge.read_json(root/'conversion-unit.json')
            receipt['unit_meta']['unknown_typed']['integer'] += 1;receipt.pop('digest');receipt['digest'] = bridge.digest(receipt)
            (root/'conversion-unit.json').write_bytes(bridge.canonical_json(receipt))
            with self.assertRaises(bridge.ContractError):bridge.commit(plan,[root]+roots[1:])

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(BridgeTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    unchanged = all(Path(path).is_file() and hashlib.sha256(Path(path).read_bytes()).hexdigest()==checksum for path,checksum in controls.items())
    report = dict(passed=result.wasSuccessful() and unchanged,tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        private_case_directories=len(cases),protected_files=len(controls),native_and_published_unit_files_unchanged=unchanged,
        converter_parity_claimed=False, owner_source_commit=distribution['source_commit'], owner_version=version,
        original_bindings_sha256=hashlib.sha256(args.bindings.read_bytes()).hexdigest(),
        original_recordings_sha256=hashlib.sha256(args.recordings.read_bytes()).hexdigest())
    (args.output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    if not report['passed']:raise SystemExit(1)


if __name__ == '__main__':main()
