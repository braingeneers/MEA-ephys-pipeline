#!/usr/bin/env python3
"""Check explicit source origins with real SI objects across a scratch copy/move.

These are tiny constructed recording/sorting artifacts, not a Kilosort execution.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys

import numpy as np
import spikeinterface as si

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'pipeline/helpers'))
import portable_bundle as portable
import producer_locations as locations


def check(output, nwb=None):
    output.mkdir(exist_ok=False)
    inputs = output / 'inputs'
    inputs.mkdir()
    if nwb:
        import spikeinterface.extractors as se
        shutil.copyfile(nwb, inputs / 'recording.nwb')
        raw = se.read_nwb_recording(inputs / 'recording.nwb')
    else:
        data = np.arange(4000, dtype='int16').reshape(2000, 2)
        data.tofile(inputs / 'raw.bin')
        raw = si.BinaryRecordingExtractor(file_paths=[inputs/'raw.bin'], sampling_frequency=20000,
                                         num_channels=2, dtype='int16')
        raw.set_channel_gains([1.5, 2.0]); raw.set_channel_offsets([-1.25, 3.0])
        raw.set_property('location', np.array([[0., 0.], [20., 0.]]))
    raw.annotate(source_history={'keep': [7, 7.0, -0.0, None, False, '../historical/path']})
    producer = output / 'scratch/process/capsule'
    source = producer / 'results'; source.mkdir(parents=True)
    layout = producer / 'data'; layout.mkdir()
    (layout / 'ecephys_session').symlink_to(inputs, target_is_directory=True)
    published = output / 'shared/work/task/capsule'
    cached = raw.save(folder=source/'preprocessed_probe', n_jobs=1)
    cached.dump_to_json(source/'binary_probe.json', relative_to=source)
    graph = raw.select_channels(raw.channel_ids[-1:])
    graph.dump_to_json(source/'preprocessed_probe.json', relative_to=source)
    graph.dump_to_pickle(source/'preprocessed_pickle.pkl', relative_to=source)
    # This input-style graph intentionally traverses a recorded input symlink.
    input_graph = raw.to_dict(recursive=True, relative_to=layout)
    key = 'file_path' if nwb else 'file_paths'
    input_graph['kwargs'][key] = 'ecephys_session/recording.nwb' if nwb else ['ecephys_session/raw.bin']
    input_graph['relative_paths'] = True
    portable.write_serialization(source/'job_0.json', {'recording_name':'probe','recording_dict':input_graph})
    viz = {'probe': {'drift': {'recording':graph.to_dict(recursive=True, relative_to=source)},
        'timeseries': {'full': {'raw':raw.to_dict(recursive=True, relative_to=layout),
                                'highpass':graph.to_dict(recursive=True, relative_to=layout)},
                       'proc': {'highpass':graph.to_dict(recursive=True, relative_to=layout),
                                'cmr':cached.to_dict(recursive=True, relative_to=layout)}}}}
    portable.write_serialization(source/'preprocessedviz_probe.json', viz)
    spikes = np.array([100, 500, 1000], dtype='int64')
    labels = np.zeros(len(spikes), dtype='int64')
    sorting = si.NumpySorting.from_samples_and_labels([spikes], [labels], raw.get_sampling_frequency(),
                                                     unit_ids=np.array([0], dtype='int64'))
    sorting.save(folder=source/'spikesorted_probe')
    analyzer = si.create_sorting_analyzer(sorting=sorting, recording=raw, sparse=False,
                                         format='zarr', folder=source/'postprocessed_probe.zarr')
    analyzer.compute('random_spikes', method='all')
    analyzer.compute('waveforms', n_jobs=1)
    analyzer.compute('templates')
    descriptors = portable.capsule_serializations(source, 'data', layout)
    expected = [portable.fingerprint(row, output) for row in descriptors]
    record = dict(schema_version=1, document_type='mea_producer_location', role='preprocessing',
        recorder_sha256='1'*64, producer_root=str(producer), published_root=str(published),
        published_results=str(published.parent/'unit-results'),
        links=[dict(source=str(layout/'ecephys_session'), target=str(inputs))])
    identity = hashlib.sha256((str(published)+'\0preprocessing').encode()).hexdigest()[:24]
    marker = source/(locations.PREFIX+identity+'.json')
    marker.write_bytes(portable.encoded(record)+b'\n')
    shutil.copytree(producer, published)
    shutil.copytree(source, published.parent/'unit-results')
    producer.parent.rename(output/'retained-scratch')
    source, layout = published.parent/'unit-results', published/'data'
    origin = locations.load(source, '1'*64)
    assert origin is not None
    descriptors = portable.capsule_serializations(source, 'data', layout)
    spec = dict(schema_version=1, stage='nwb', unit_id='origin-probe', recording_ids=['probe'],
        payloads=[dict(source=str(inputs), destination='data/ecephys_session'),
                  dict(source=str(source), destination='data')], serializations=descriptors,
        dependency_aliases=[dict(source=str(layout/'ecephys_session'), destination='data/ecephys_session')])
    try:
        portable.pack(spec, output/'missing-origin')
    except portable.BundleError as error:
        assert 'Missing recording dependency' in str(error), str(error)
    else:
        raise AssertionError('The unrecorded old scratch base unexpectedly survived')
    for descriptor in descriptors:
        descriptor['producer_location'] = origin
    packed = output/'packed'
    manifest = portable.pack(spec, packed)
    portable.verify(packed)
    assert portable.encoded(manifest['scientific_fingerprints']) == portable.encoded(expected)
    assert all('producer_location' not in row for row in manifest['serializations'])
    assert all('producer_location' in row for row in manifest['original_serializations'])
    # An origin cannot be detached from its exact copied provenance bytes.
    changed = deepcopy(spec)
    changed['serializations'][0]['producer_location']['record']['links'][0]['target'] = str(output/'undeclared')
    try:
        portable.pack(changed, output/'changed-origin')
    except (portable.BundleError, locations.LocationError) as error:
        assert 'Producer location' in str(error) or 'producer location' in str(error), str(error)
    else:
        raise AssertionError('Changed origin metadata bypassed its inventory binding')
    negatives = []
    for field, value in [('schema_version', True), ('schema_version', 1.0),
                         ('producer_root', 'relative/root'), ('published_results', str(output/'wrong-port')),
                         ('role', ['preprocessing'])]:
        bad = deepcopy(record); bad[field] = value
        try:
            locations.validate(bad)
        except locations.LocationError:
            negatives.append(field + ':' + repr(value))
        else:
            raise AssertionError('Malformed origin was accepted')
    try:
        locations.decode(b'{"schema_version":1,"schema_version":1}')
    except locations.LocationError as error:
        assert 'Duplicate' in str(error)
        negatives.append('duplicate-json-field')
    else:
        raise AssertionError('Contradictory duplicate origin fields were accepted')
    bad = deepcopy(record); bad['links'].append(deepcopy(bad['links'][0]))
    try:
        locations.validate(bad)
    except locations.LocationError as error:
        assert 'ambiguous' in str(error)
        negatives.append('ambiguous-link')
    else:
        raise AssertionError('Duplicate link was accepted')
    try:
        locations.load(output / 'inputs', '1'*64)
    except locations.LocationError as error:
        assert 'Missing required' in str(error)
        negatives.append('missing-required-record')
    else:
        raise AssertionError('Missing current producer record was accepted')
    bad_graph = deepcopy(input_graph); bad_graph['kwargs']['unreviewed_path'] = str(inputs)
    try:
        portable.source_extractor(bad_graph, layout, origin)
    except portable.BundleError as error:
        assert 'Unsupported executable path field' in str(error)
        negatives.append('unsupported-executable-path')
    else:
        raise AssertionError('Unknown executable field bypassed the strict visitor')
    rogue = output / 'rogue'; shutil.copytree(inputs, rogue)
    control = output / 'wrong-link/capsule'
    shutil.copytree(published, control)
    control_results = control.parent / 'unit-results'
    shutil.copytree(source, control_results)
    for path in control_results.glob(locations.PREFIX + '*.json'):
        path.unlink()
    wrong = deepcopy(record); wrong['published_root'] = str(control)
    wrong['published_results'] = str(control_results)
    wrong['links'][0]['target'] = str(rogue)
    identity = hashlib.sha256((str(control)+'\0preprocessing').encode()).hexdigest()[:24]
    (control_results / (locations.PREFIX+identity+'.json')).write_bytes(portable.encoded(wrong)+b'\n')
    wrong_origin = locations.load(control_results, '1'*64)
    wrong_descriptors = portable.capsule_serializations(control_results, 'data', control / 'data')
    for descriptor in wrong_descriptors:
        descriptor['producer_location'] = wrong_origin
    wrong_spec = dict(spec, payloads=[dict(source=str(inputs), destination='data/ecephys_session'),
        dict(source=str(control_results), destination='data')], serializations=wrong_descriptors,
        dependency_aliases=[dict(source=str(control / 'data/ecephys_session'), destination='data/ecephys_session')])
    try:
        portable.pack(wrong_spec, output / 'undeclared-origin-target')
    except portable.BundleError as error:
        assert 'Undeclared recording dependency' in str(error), str(error)
        negatives.append('undeclared-origin-target')
    else:
        raise AssertionError('A resealed origin authorized a file outside declared payloads')
    result = dict(passed=True, negative_controls=negatives, formats=sorted({row['format'] for row in descriptors}),
        descriptors=len(descriptors), complete_scientific_fingerprints_exact=True,
        original_missing_base_reproduced=True, origin_inventory_mismatch_rejected=True,
        actual_scratch_retired=True, input_symlink_resolved=True, source_nwb=bool(nwb),
        scientific_capsules_constructed=True, actual_ks4=False)
    (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--nwb',type=Path)
    args=parser.parse_args();check(args.output.resolve(),args.nwb)
