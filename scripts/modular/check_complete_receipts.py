#!/usr/bin/env python3
"""Bounded v2 contract tests on generated acquisitions, not KS4 parity evidence.

Run in the pinned base scientific image. Outputs and failed controls stay in the
explicit external directory; original full-size acceptance is a separate check.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest

import h5py
import numpy as np
import spikeinterface as si
import spikeinterface.extractors as se

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / 'pipeline/helpers'))
import portable_bundle as portable
import stage_bundle as stages

ROOT = None
INPUT_NWB = None


def reseal(root, record=None):
    record = record if record is not None else stages.read_record(root)
    record['files'] = portable.inventory(root / 'data')
    record['bytes'] = sum(item['bytes'] for item in record['files'].values())
    record.pop('content_sha256', None)
    record['content_sha256'] = hashlib.sha256(portable.encoded(record)).hexdigest()
    (root / 'bundle.json').write_bytes(portable.encoded(record) + b'\n')
    return record


class CompleteReceipts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = ROOT / 'original'
        cls.source.mkdir()
        cls.values = (np.arange(140003 * 2).reshape(-1, 2) % 30000).astype('int16')
        shutil.copyfile(INPUT_NWB, cls.source / 'input.nwb')
        with h5py.File(cls.source / 'input.nwb', 'r') as nwb:
            np.testing.assert_array_equal(nwb['acquisition/ElectricalSeries/data'][:], cls.values)
        recording = se.read_nwb_recording(cls.source / 'input.nwb')
        recording.dump_to_json(cls.source / 'recording.json')
        recording.dump_to_pickle(cls.source / 'recording.pkl')
        graph = portable.read_serialization(cls.source / 'recording.json')
        (cls.source / 'job_0.json').write_bytes(portable.serialized_json({'recording_name': 'r', 'recording_dict': graph}))
        descriptors = [dict(format='extractor_json' if name.endswith('.json') else 'extractor_pickle',
                            source=str(cls.source / name), base=str(cls.source), path='data/' + name,
                            relative_to='data', pointer=pointer)
                       for name, pointer in [('recording.json', []), ('recording.pkl', []), ('job_0.json', ['recording_dict'])]]
        cls.spec = dict(schema_version=1, stage='nwb', unit_id='unit', recording_ids=['r'],
                        payloads=[dict(source=str(cls.source), destination='data')], serializations=descriptors,
                        metadata=dict(phase='dispatch', unit_meta={'unit_id': 'unit'}, max_duration_minutes=1,
                                      completed_stages=['dispatch'], stage_provenance=[dict(phase='dispatch',
                                      processes=[], context={'workflow_version': 'generated-fixture'})]))
        cls.baseline = ROOT / 'baseline'
        record = portable.pack(cls.spec, cls.baseline)
        record['metadata']['outcomes'] = stages.scientific_policy(cls.baseline, record, lambda log: False)
        cls.record = reseal(cls.baseline, record)
        cls.before = portable.inventory(cls.baseline / 'data')
        stages.verify(cls.baseline, 'nwb', 'unit', lambda log: False, unit_meta={'unit_id': 'unit'})

    def copy(self):
        target = ROOT / self._testMethodName
        shutil.copytree(self.baseline, target)
        return target

    def test_complete_multisegment_samples_scaling_and_clocks(self):
        arrays = [np.arange(131089 * 3, dtype='int16').reshape(-1, 3), np.zeros((1003, 3), dtype='int16')]
        rec = si.NumpyRecording(arrays, 30000.1, t_starts=[0., 0.000003])
        rec.set_channel_gains([2.5, 1., .25])
        rec.set_channel_offsets([0., -1., 10.])
        times = np.arange(len(arrays[0]), dtype='float64') / 30000.1
        times[70000:] += .001
        rec.set_times(times, segment_index=0, with_warning=False)
        actual = portable.recording_identity(rec)
        for segment, array in enumerate(arrays):
            item = actual['segments'][segment]
            self.assertEqual(item['raw'], portable.array_identity(array))
            self.assertEqual(item['microvolts'], portable.array_identity(rec.get_traces(segment_index=segment, return_scaled=True)))
            self.assertEqual(item['times'], portable.array_identity(rec.get_times(segment_index=segment)))
        arrays[0][123456, 0] += 1
        self.assertNotEqual(actual['segments'][0]['raw'], portable.recording_identity(rec)['segments'][0]['raw'])
        times[123456] += .00001
        rec.set_times(times, segment_index=0, with_warning=False)
        self.assertNotEqual(actual['segments'][0]['times'], portable.recording_identity(rec)['segments'][0]['times'])

    def test_stream_empty_and_exact_float_bits(self):
        values = np.array([[0., -0.], [np.nan, np.inf], [-np.inf, 1e-300]], dtype='float64')
        for array in [values, values[:0], values[:, ::-1]]:
            self.assertEqual(portable.array_identity(array), portable.streamed_array_identity(array.shape, lambda a, b: array[a:b]))
        other = values.copy()
        other[0, 1] = 0.
        self.assertNotEqual(portable.array_identity(values), portable.array_identity(other))

    def test_same_value_float_schema_rejected(self):
        root = self.copy()
        record = stages.read_record(root)
        record['schema_version'] = 2.0
        reseal(root, record)
        with self.assertRaisesRegex(portable.BundleError, 'schema_version must be exact integer 2'):
            portable.verify(root)

    def test_legacy_error_is_actionable(self):
        root = self.copy()
        record = stages.read_record(root)
        record.update(schema_version=1, contract='mea.local-stage-bundle/v1')
        reseal(root, record)
        with self.assertRaisesRegex(portable.BundleError, 're-run its named stage producer'):
            portable.verify(root)

    def test_spec_and_request_versions_are_separate_exact_integers(self):
        for value in [True, 1.0, 2]:
            with self.assertRaisesRegex(portable.BundleError, 'assembly schema'):
                portable.pack(dict(self.spec, schema_version=value), ROOT / ('bad-spec-' + repr(value)))
            with self.assertRaisesRegex(portable.BundleError, 'stage assembly request'):
                stages.build(dict(schema_version=value, phase='dispatch'), [], ROOT / 'bad-request', lambda log: False)

    def test_changed_nwb_sample_outside_old_windows_rejected(self):
        root = self.copy()
        with h5py.File(root / 'data/input.nwb', 'r+') as nwb:
            dataset = nwb['acquisition/ElectricalSeries/data']
            dataset[123456, 0] = int(dataset[123456, 0]) + 1
        reseal(root)
        with self.assertRaisesRegex(portable.BundleError, 'scientific fingerprint mismatch'):
            portable.verify(root)

    def test_header_change_with_exact_recording_values_rejected(self):
        root = self.copy()
        with h5py.File(root / 'data/input.nwb', 'r+') as nwb:
            nwb['identifier'][()] = 'receipt-changed'
        reseal(root)
        # The independent original complete values are frozen before mutation.
        before = self.record['scientific_fingerprints']
        after = [portable.fingerprint(d, root, portable=True) for d in self.record['serializations']]
        self.assertEqual(portable.encoded(before), portable.encoded(after))
        with self.assertRaisesRegex(portable.BundleError, 'executable binding mismatch'):
            portable.verify(root)

    def test_rebound_identical_source_rejected(self):
        root = self.copy()
        (root / 'data/other').mkdir()
        shutil.copyfile(root / 'data/input.nwb', root / 'data/other/input.nwb')
        path = root / 'data/recording.json'
        graph = portable.read_serialization(path)
        graph['kwargs']['file_path'] = 'other/input.nwb'
        portable.write_serialization(path, graph)
        reseal(root)
        with self.assertRaisesRegex(portable.BundleError, 'executable binding mismatch'):
            portable.verify(root)

    def test_absolute_path_to_same_source_rejected_before_constructor(self):
        root = self.copy()
        path = root / 'data/job_0.json'
        value = portable.read_serialization(path)
        graph = value['recording_dict']
        graph['kwargs']['file_path'] = str(root / 'data/input.nwb')
        for probe_constructor in [False, True]:
            if probe_constructor:
                # If policy constructs this graph before path preflight, an
                # unrelated constructor error would hide the intended guard.
                graph['kwargs']['unknown_constructor_probe'] = True
            portable.write_serialization(path, value)
            reseal(root)
            with self.assertRaisesRegex(portable.BundleError, 'requires relative filesystem paths'):
                portable.verify(root)
            with self.assertRaisesRegex(portable.BundleError, 'requires relative filesystem paths'):
                stages.verify(root, 'nwb', 'unit', lambda log: False)

    def test_selector_type_and_kwargs_order_rejected(self):
        for name in ['type', 'order']:
            root = ROOT / (self._testMethodName + '-' + name)
            shutil.copytree(self.baseline, root)
            path = root / 'data/recording.json'
            graph = portable.read_serialization(path)
            if name == 'type':
                self.assertIs(graph['kwargs']['load_time_vector'], False)
                graph['kwargs']['load_time_vector'] = 0
            else:
                graph['kwargs'] = dict(reversed(list(graph['kwargs'].items())))
            portable.write_serialization(path, graph)
            reseal(root)
            self.assertEqual(portable.encoded(self.record['scientific_fingerprints']), portable.encoded(
                [portable.fingerprint(d, root, portable=True) for d in self.record['serializations']]))
            with self.assertRaisesRegex(portable.BundleError, 'executable binding mismatch'):
                portable.verify(root)

    def test_receipt_types_rejected(self):
        for value in [140003.0, True]:
            root = ROOT / (self._testMethodName + '-' + repr(value))
            shutil.copytree(self.baseline, root)
            record = stages.read_record(root)
            record['metadata']['outcomes']['r']['frames'][0] = value
            reseal(root, record)
            with self.assertRaisesRegex(portable.BundleError, 'Scientific outcome receipt differs'):
                stages.verify(root, 'nwb', 'unit', lambda log: False)

    def test_incoming_native_declaration_requires_receipt(self):
        with self.assertRaisesRegex(portable.BundleError, 'Native conversion identity:'):
            stages.verify(self.baseline, 'nwb', 'unit', lambda log: False,
                          unit_meta={'unit_id': 'unit', 'maxwell_conversion': {}})

    def test_reserved_receipt_cannot_opt_out(self):
        root = self.copy()
        (root / 'data/ecephys_session').mkdir()
        (root / 'data/ecephys_session/conversion-unit.json').write_text('{}')
        reseal(root)
        with self.assertRaisesRegex(portable.BundleError, 'Native conversion identity:'):
            stages.verify(root, 'nwb', 'unit', lambda log: False)

    def test_metadata_rejected_before_scientific_read(self):
        root = self.copy()
        # This graph cannot load; incoming metadata must still be checked first.
        path = root / 'data/recording.json'
        graph = portable.read_serialization(path)
        graph['kwargs']['file_path'] = 'missing.nwb'
        portable.write_serialization(path, graph)
        reseal(root)
        with self.assertRaisesRegex(portable.BundleError, 'dropped or changed predecessor'):
            stages.verify(root, 'nwb', 'unit', lambda log: False, unit_meta={'unit_id': 'other'})

    def test_historical_annotations_and_relocation_preserved(self):
        root = self.copy()
        path = root / 'data/recording.json'
        graph = portable.read_serialization(path)
        graph.setdefault('annotations', {})['history'] = {'file_path': '/unavailable/original.nwb'}
        portable.write_serialization(path, graph)
        reseal(root)
        self.assertTrue(portable.verify(root)['passed'])
        self.assertEqual(portable.inventory(self.baseline / 'data'), self.before)


def create_nwb(path):
    """Create once in the pinned NWB image; the base image only consumes it."""
    from pynwb import NWBFile, NWBHDF5IO, validate
    from pynwb.ecephys import ElectricalSeries

    if path.exists():
        raise FileExistsError(path)
    nwb = NWBFile(session_description='generated contract fixture', identifier='receipt-original',
                  session_start_time=datetime(2020, 1, 1, tzinfo=timezone.utc))
    device = nwb.create_device('device')
    group = nwb.create_electrode_group('group', 'fixture', 'culture', device)
    for channel in range(2):
        nwb.add_electrode(id=channel, x=float(channel), y=0., z=0., imp=np.nan,
                          location='culture', filtering='none', group=group)
    table = nwb.create_electrode_table_region([0, 1], 'channels')
    values = (np.arange(140003 * 2).reshape(-1, 2) % 30000).astype('int16')
    nwb.add_acquisition(ElectricalSeries(name='ElectricalSeries', data=values, electrodes=table,
                                       rate=30000.1, conversion=2.5e-6))
    with NWBHDF5IO(str(path), 'w') as io:
        io.write(nwb)
    with NWBHDF5IO(str(path), 'r') as io:
        assert not validate(io=io)


def main():
    global ROOT, INPUT_NWB
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create-nwb', help='Run in the pinned NWB image')
    create.add_argument('--output', type=Path, required=True)
    check = commands.add_parser('verify', help='Run in the pinned base image')
    check.add_argument('--source-nwb', type=Path, required=True)
    check.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'create-nwb':
        create_nwb(args.output)
        return
    INPUT_NWB = args.source_nwb.resolve(strict=True)
    ROOT = args.output.resolve()
    ROOT.mkdir(parents=True, exist_ok=False)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CompleteReceipts))
    report = dict(passed=result.wasSuccessful(), tests=result.testsRun,
                  errors=[dict(test=str(test), traceback=detail) for test, detail in result.errors],
                  failures=[dict(test=str(test), traceback=detail) for test, detail in result.failures],
                  scope='Generated technical acquisitions only; full-size science and real Nextflow acceptance are separate')
    (ROOT / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
    raise SystemExit(not result.wasSuccessful())


if __name__ == '__main__':
    main()
