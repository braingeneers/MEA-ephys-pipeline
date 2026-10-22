#!/usr/bin/env python3
"""Check dispatch recording serialization across Nextflow scratch relocation.

Run in the pinned base scientific image with --output set to a fresh directory.
No sorter or scientific defaults are replaced by this serialization regression.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import numpy as np
import spikeinterface as si


SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / 'pipeline/helpers'))
import portable_bundle as portable

spec = importlib.util.spec_from_file_location(
    'dispatch_capsule', SOURCE / 'capsules/job_dispatch/code/run_capsule.py')
dispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatch)


def exact_arrays(actual, expected):
    assert actual.dtype == expected.dtype, (actual.dtype, expected.dtype)
    assert actual.shape == expected.shape, (actual.shape, expected.shape)
    np.testing.assert_array_equal(actual, expected)


def check(output, nwb=None):
    output.mkdir(parents=True, exist_ok=False)
    raw = output / 'shared/source' / ('recording.nwb' if nwb else 'recording.bin')
    raw.parent.mkdir(parents=True)
    if nwb:
        import spikeinterface.extractors as se
        shutil.copyfile(nwb, raw)
        recording = se.read_nwb_recording(raw)
        samples = recording.get_traces()
    else:
        samples = np.arange(256, dtype='int16').reshape(128, 2)
        samples.tofile(raw)
        recording = si.BinaryRecordingExtractor(
            file_paths=[raw], sampling_frequency=20000, num_channels=2, dtype='int16')
        recording.set_property('location', np.array([[0., 0.], [20., 0.]]))
        recording.set_channel_gains([1.5, 2.0])
        recording.set_channel_offsets([-1.25, 3.0])
    scratch = output / 'scratch/process'
    old_base = scratch / 'capsule/data'
    old_base.mkdir(parents=True)
    moved_base = output / 'shared/work/dispatch/capsule/data'
    moved_base.mkdir(parents=True)
    recording.annotate(source_history={'keep': ['relative/provenance', 7, 7.0, None]})
    recordings = [recording, recording.select_channels(recording.channel_ids[-1:])]
    original = recording.to_dict(recursive=True, relative_to=old_base)
    np.testing.assert_array_equal(si.load(original, base_folder=old_base).get_traces(), samples)
    serialized = [(rec, annotations, dispatch.serialize_recording(rec, include_annotations=annotations))
                  for rec in recordings for annotations in (False, True)]
    scratch.rename(output / 'retained-scratch')
    try:
        si.load(original, base_folder=moved_base).get_traces()
    except (FileNotFoundError, AssertionError, ValueError, RuntimeError) as error:
        assert raw.name in str(error), str(error)
    else:
        raise AssertionError('Original scratch-relative reference unexpectedly survived relocation')
    root = output / 'bundle'
    target = root / 'data/ecephys_session' / raw.name
    target.parent.mkdir(parents=True)
    shutil.copyfile(raw, target)
    results = []
    for index, (rec, annotations, value) in enumerate(serialized):
        assert value['relative_paths'] is False
        loaded = si.load(value, base_folder=moved_base)
        rebased = portable.rebase_extractor(
            value, moved_base, root / 'data', {raw.resolve(): target.resolve()}, root)
        portable.assert_local_extractor(rebased, root / 'data', root)
        for extension in ('.json', '.pkl'):
            path = root / ('recording-' + str(index) + extension)
            portable.write_serialization(path, rebased)
            restored = si.load(portable.read_serialization(path), base_folder=root / 'data')
            for actual in (loaded, restored):
                exact_arrays(actual.get_traces(), rec.get_traces())
                exact_arrays(actual.get_traces(return_scaled=True), rec.get_traces(return_scaled=True))
                exact_arrays(actual.get_times(), rec.get_times())
                exact_arrays(actual.get_channel_locations(), rec.get_channel_locations())
                exact_arrays(actual.get_channel_ids(), rec.get_channel_ids())
                assert actual.get_dtype() == rec.get_dtype()
                assert actual.get_sampling_frequency() == rec.get_sampling_frequency()
                if annotations:
                    assert portable.encoded(actual.get_annotation('source_history')) == portable.encoded(
                        rec.get_annotation('source_history'))
            assert rebased['relative_paths'] is True
        try:
            portable.rebase_extractor(value, moved_base, root / 'data', {}, root)
        except portable.BundleError as error:
            assert 'Undeclared recording dependency' in str(error)
        else:
            raise AssertionError('Undeclared producer dependency accepted')
        results.append({'nested_selection': rec is not recording, 'annotations': annotations,
                        'json_pickle_full_readback': 'passed'})
    # The optional LFP is an executable recording too. Both job references must
    # be declared/rebased, including the capsule's JSON and pickle alternatives.
    for suffix in ('.json', '.pkl'):
        producer = output / ('jobs-' + suffix[1:])
        producer.mkdir()
        job = {'recording_name': 'scratch-recording',
               'recording_dict': dispatch.serialize_recording(recordings[0]),
               'recording_lfp_dict': dispatch.serialize_recording(recordings[1])}
        portable.write_serialization(producer / ('job_0' + suffix), job)
        descriptors = portable.capsule_serializations(producer, 'data', moved_base)
        assert {tuple(item['pointer']) for item in descriptors} == {
            ('recording_dict',), ('recording_lfp_dict',)}
        copied_input = output / ('copied-input-' + suffix[1:])
        shutil.copytree(raw.parent, copied_input)
        packing = {'schema_version': 1, 'stage': 'nwb', 'unit_id': 'scratch-unit',
            'recording_ids': ['scratch-recording'],
            'payloads': [{'source': str(raw.parent), 'destination': 'data/ecephys_session'},
                         {'source': str(producer), 'destination': 'data'}],
            'serializations': descriptors}
        copied_only = dict(packing, payloads=[
            {'source': str(copied_input), 'destination': 'data/ecephys_session'},
            {'source': str(producer), 'destination': 'data'}])
        try:
            portable.pack(copied_only, output / ('undeclared-original-' + suffix[1:]))
        except portable.BundleError as error:
            assert 'Undeclared recording dependency' in str(error)
        else:
            raise AssertionError('A same-byte copy silently authorized an undeclared original path')
        # The workflow must supply the original declared unit, not guess its
        # location from producer output. Its copied layout is independently bound.
        packing['dependency_aliases'] = [
            {'source': str(copied_input), 'destination': 'data/ecephys_session'}]
        packed = output / ('packed-' + suffix[1:])
        manifest = portable.pack(packing, packed)
        portable.verify(packed)
        assert len(manifest['scientific_fingerprints']) == 2
        for pointer in ('recording_dict', 'recording_lfp_dict'):
            assert portable.read_serialization(packed / 'data' / ('job_0' + suffix))[pointer]['relative_paths'] is True
        changed_copy = copied_input / raw.name
        with changed_copy.open('r+b') as stream:
            first = stream.read(1)
            stream.seek(0)
            stream.write(bytes([first[0] ^ 1]))
        try:
            portable.register_alias(changed_copy, packed / 'data/ecephys_session' / raw.name, {})
        except portable.BundleError as error:
            assert 'Dependency alias content differs' in str(error)
        else:
            raise AssertionError('A changed producer copy was accepted as the declared input')
    (output / 'result.json').write_text(json.dumps({
        'original_scratch_relative_failure_reproduced': True,
        'actual_dispatch_serializer': results, 'published_relative_only': True,
        'complete_samples_clocks_geometry_dtype_ids_exact': True,
        'undeclared_dependencies_rejected': True,
        'copied_layout_missing_original_rejected': True,
        'original_input_and_copy_explicitly_bound': True,
        'changed_copy_rejected': True,
        'main_and_optional_lfp_complete_pack_verify': 'json and pickle passed'}, indent=2) + '\n')
    print((output / 'result.json').read_text())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--nwb', type=Path, help='Optional existing self-contained NWB from the pinned writer')
    args = parser.parse_args()
    check(args.output.resolve(), args.nwb)
