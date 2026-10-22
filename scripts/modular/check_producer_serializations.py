#!/usr/bin/env python3
"""Real SI producer/collector relocation controls; no Kilosort execution."""
import argparse
import ast
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import shutil
import sys

import numcodecs
import numpy as np
import spikeinterface as si
from spikeinterface.core.core_tools import extractor_dict_iterator, set_value_in_extractor_dict
import zarr

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / 'pipeline/helpers'))
import portable_bundle as portable
import producer_serializations


def metadata_signature(value):
    """Only executable dependency values and their path modes may change."""
    value = deepcopy(value)

    def visit(node):
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            if 'class' not in node:
                for child in node.values():
                    visit(child)
                return
            if 'relative_paths' in node:
                assert type(node['relative_paths']) is bool
                node['relative_paths'] = '<path-mode>'
            if node.get('folder_metadata') is not None:
                node['folder_metadata'] = '<dependency>'
            for key, child in node['kwargs'].items():
                if key in {'file_path', 'file_paths', 'folder_path', 'folder_metadata'} and child is not None:
                    node['kwargs'][key] = ['<dependency>'] * len(child) if isinstance(child, list) else '<dependency>'
                else:
                    visit(child)

    visit(value)
    return portable.serialized_json(value)


def stored_graph(descriptor):
    if descriptor['format'] == 'analyzer_zarr':
        return zarr.open(descriptor['source'], mode='r')['recording'][0]
    return portable.at_pointer(portable.read_serialization(Path(descriptor['source'])), descriptor.get('pointer', []))


def descriptor_key(descriptor):
    return descriptor['path'] + ':' + json.dumps(descriptor.get('pointer', []))


def rejected_controls(root, inputs, raw):
    outcomes = []
    for kind in ('missing', 'undeclared', 'unknown-field', 'relative-flag', 'results-link'):
        producer = root/kind/'capsule'; results = producer/'results'; results.mkdir(parents=True)
        data = producer/'data'; data.mkdir(); (data/'ecephys_session').symlink_to(inputs, target_is_directory=True)
        value = raw.to_dict(recursive=True)
        if kind in ('missing', 'undeclared'):
            target = inputs/'absent' if kind == 'missing' else root/'foreign.bin'
            if kind == 'undeclared':
                target.write_bytes(b'foreign-data')
            value = portable.map_extractor_dependencies(value, lambda path: str(target), False)
        elif kind == 'unknown-field':
            value['kwargs']['unreviewed_path'] = str(inputs)
        elif kind == 'relative-flag':
            value = portable.map_extractor_dependencies(value, lambda path: 'ecephys_session/missing', False)
        portable.write_serialization(results/'job_0.json', {'recording_dict':value})
        if kind == 'results-link':
            # Retain the owned directory; never remove or modify the input.
            results.rename(producer/'retained-results')
            results.symlink_to(inputs, target_is_directory=True)
        published = root/'controls-shared'/kind/'capsule'
        try:
            producer_serializations.normalize(producer, published, published.parent/'unit-results',
                'job_dispatch', [dict(source=str(data/'ecephys_session'), target=str(inputs))])
        except (portable.BundleError, FileNotFoundError) as error:
            expected = {'missing':'No such file', 'undeclared':'not a declared staged input',
                        'unknown-field':'Unsupported executable path field',
                        'relative-flag':'relative dependency without', 'results-link':'results escape'}[kind]
            assert expected in str(error), (kind,str(error))
            outcomes.append(kind)
        else:
            raise AssertionError('Invalid producer metadata accepted: '+kind)
    return outcomes


def remapper():
    path = SOURCE / 'capsules/results_collector/code/run_capsule.py'
    tree = ast.parse(path.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'remap_extractor_path')
    scope = dict(Path=Path, os=os, logging=logging,
                 extractor_dict_iterator=extractor_dict_iterator,
                 set_value_in_extractor_dict=set_value_in_extractor_dict)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), scope)
    return scope['remap_extractor_path']


def staged(producer, published):
    shutil.copytree(producer, published)
    shutil.copytree(producer / 'results', published.parent / 'unit-results')
    producer.parent.rename(producer.parent.with_name(producer.parent.name + '-retired'))
    return published.parent / 'unit-results'


def check_case(root, codec, nwb):
    root.mkdir()
    inputs = root / 'inputs'; inputs.mkdir()
    if nwb:
        shutil.copyfile(nwb, inputs / 'recording.nwb')
        import spikeinterface.extractors as se
        raw = se.read_nwb_recording(inputs / 'recording.nwb')
        raw = raw.frame_slice(0, min(2000, raw.get_num_frames()))
    else:
        np.arange(4000, dtype='int16').reshape(2000, 2).tofile(inputs / 'raw.bin')
        raw = si.BinaryRecordingExtractor(file_paths=[inputs/'raw.bin'], sampling_frequency=20000,
                                         num_channels=2, dtype='int16')
        raw.set_channel_gains([1.5, 2.0]); raw.set_channel_offsets([-1.25, 3.0])
        raw.set_property('location', np.array([[0., 0.], [20., 0.]]))
    raw.annotate(source_history={'keep': [7, 7.0, -0.0, None, False, '../historical/path']})
    input_hashes = {p.name: portable.sha256(p) for p in inputs.iterdir()}
    producer = root / 's/capsule'; results = producer / 'results'; results.mkdir(parents=True)
    data = producer / 'data'; data.mkdir(); (data / 'ecephys_session').symlink_to(inputs, target_is_directory=True)
    published = root / 'shared/runs/test/work/deep/producer/task/capsule'
    links = [dict(source=str(data/'ecephys_session'), target=str(inputs))]
    cached = raw.save(folder=results/'preprocessed_probe', n_jobs=1)
    cached.dump_to_json(results/'binary_probe.json', relative_to=results)
    raw.dump_to_pickle(results/'preprocessed_probe.pkl', relative_to=results)
    job_graph = raw.to_dict(recursive=True, relative_to=data)
    job_graph.setdefault('annotations', {})['probe_history'] = {'typed':[7,7.0,-0.0,False,None],
                                                              'path':'../historical/path'}
    portable.write_serialization(results/'job_0.json', dict(recording_name='probe', recording_dict=job_graph,
        recording_lfp_dict=raw.select_channels(raw.channel_ids[-1:]).to_dict(recursive=True,relative_to=data)))
    sorting = si.NumpySorting.from_samples_and_labels(
        [np.array([100, 500, 1000], dtype='int64')], [np.zeros(3, dtype='int64')],
        raw.get_sampling_frequency(), unit_ids=np.array([0], dtype='int64'))
    analyzer_path = results / 'postprocessed_probe.zarr'
    analyzer = si.create_sorting_analyzer(sorting=sorting, recording=raw, sparse=False,
                                         format='zarr', folder=analyzer_path)
    analyzer.compute('random_spikes', method='all'); analyzer.compute('waveforms', n_jobs=1)
    analyzer.compute('templates')
    group = zarr.open(str(analyzer_path), mode='a'); graph = group['recording'][0]
    del group['recording']
    group.create_dataset('recording', data=np.array([portable.jsonable(graph) if codec == 'json' else graph], dtype=object),
                         object_codec=numcodecs.JSON() if codec == 'json' else numcodecs.Pickle())
    zarr.consolidate_metadata(group.store)
    descriptors = portable.capsule_serializations(results, 'data', data)
    before = {descriptor_key(row): portable.fingerprint(row, root) for row in descriptors}
    before_metadata = {descriptor_key(row): metadata_signature(stored_graph(row)) for row in descriptors}
    # A copy-on-write metadata update must leave a hard-linked source untouched.
    protected = root / 'protected'; protected.mkdir()
    os.link(results/'job_0.json', protected/'job.json')
    os.link(analyzer_path/'recording/0', protected/'zarr-recording')
    protected_hashes = {p.name: portable.sha256(p) for p in protected.iterdir()}
    producer_serializations.normalize(producer, published, published.parent/'unit-results', 'postprocessing', links)
    output = staged(producer, published)
    after = portable.capsule_serializations(output, 'data', published/'data')
    assert portable.encoded(before) == portable.encoded({descriptor_key(row): portable.fingerprint(row, root) for row in after})
    assert before_metadata == {descriptor_key(row): metadata_signature(stored_graph(row)) for row in after}
    current = output/'postprocessed_probe.zarr'
    loaded = si.load_sorting_analyzer(current)
    assert loaded.recording is not None
    assert portable.encoded(portable.recording_identity(loaded.recording)) == portable.encoded(portable.recording_identity(raw))
    assert {p.name: portable.sha256(p) for p in protected.iterdir()} == protected_hashes

    # Execute the actual collector path operation against the staged artifact,
    # then normalize its explicit publication/input coordinate systems.
    collector = root/'c/capsule'; collected = collector/'results'; collected.mkdir(parents=True)
    collector_code = collector/'code'; collector_code.mkdir()
    collector_data = collector/'data'; collector_data.mkdir()
    (collector_data/'ecephys_session').symlink_to(inputs, target_is_directory=True)
    (collector_data/'postprocessed_probe.zarr').symlink_to(current, target_is_directory=True)
    publication = root/'not-created/products/artifacts/unit'
    shutil.copyfile(output/'job_0.json', collected/'job_0.json')
    target = collected/'postprocessed/probe.zarr'; target.parent.mkdir()
    shutil.copytree(current, target)
    incoming = zarr.open(str(current), mode='r')['recording'][0]
    publication_argument = os.path.relpath(publication, collector_code) if codec == 'json' else str(publication)
    input_argument = os.path.relpath(inputs, collector_code) if codec == 'json' else str(inputs)
    cwd = Path.cwd()
    try:
        os.chdir(collector_code)
        mapped = remapper()(deepcopy(incoming), current, Path(publication_argument)/'postprocessed/probe.zarr')
        preprocessed = remapper()(deepcopy(incoming), current, input_argument)
    finally:
        os.chdir(cwd)
    assert mapped['relative_paths'] is True
    zarr.open(str(target), mode='a')['recording'][0] = mapped
    (collected/'preprocessed').mkdir()
    portable.write_serialization(collected/'preprocessed/probe.json', preprocessed)
    collector_published = root/'shared/runs/test/work/deeper/collector/task/capsule'
    producer_serializations.normalize(collector, collector_published,
        collector_published.parent/'unit-results', 'results_collector',
        [dict(source=str(collector_data/'ecephys_session'), target=str(inputs)),
         dict(source=str(collector_data/'postprocessed_probe.zarr'), target=str(current))],
        dict(input_base=input_argument, publication_base=publication_argument))
    final = staged(collector, collector_published)
    final_analyzer = si.load_sorting_analyzer(final/'postprocessed/probe.zarr')
    assert final_analyzer.recording is not None
    assert portable.encoded(portable.recording_identity(final_analyzer.recording)) == portable.encoded(portable.recording_identity(raw))
    assert portable.encoded(portable.sorting_identity(final_analyzer.sorting)) == portable.encoded(portable.sorting_identity(sorting))
    lazy = si.load(portable.read_serialization(final/'preprocessed/probe.json'))
    assert portable.encoded(portable.recording_identity(lazy)) == portable.encoded(portable.recording_identity(raw))
    descriptions = portable.capsule_serializations(final, 'data', collector_published/'data')
    descriptions.append(dict(format='extractor_json', source=str(final/'preprocessed/probe.json'),
        path='data/preprocessed/probe.json', base=str(inputs), pointer=[], relative_to='data/ecephys_session'))
    packed = root/'packed'
    manifest = portable.pack(dict(schema_version=1, stage='analyzed', unit_id='probe-'+codec,
        recording_ids=['probe'], payloads=[dict(source=str(inputs),destination='data/ecephys_session'),
                                         dict(source=str(final),destination='data')],
        serializations=descriptions), packed)
    portable.verify(packed)
    relocated = root/'relocated'; packed.rename(relocated)
    portable.verify(relocated)
    assert {p.name: portable.sha256(p) for p in inputs.iterdir()} == input_hashes
    rejected = rejected_controls(root/'negative', inputs, raw)
    assert {p.name: portable.sha256(p) for p in inputs.iterdir()} == input_hashes
    return dict(codec=codec, producer_descriptors=len(descriptors), ordinary_analyzer_loads_after_stageout=True,
                collector_recording_resolves=True, full_scientific_identities_equal=True,
                nonpath_metadata_and_key_order_preserved=True, metadata_hardlinks_protected=True,
                original_input_hashes_unchanged=True, complete_portable_bundle_verified_after_move=True,
                rejected_controls=rejected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--nwb', type=Path)
    args = parser.parse_args(); root = args.output.resolve(); root.mkdir(exist_ok=False)
    result = dict(passed=True, scientific_sorting_constructed=True,
                  cases=[check_case(root/codec, codec, args.nwb) for codec in ('json', 'pickle')])
    (root/'result.json').write_text(json.dumps(result, indent=2)+'\n'); print(json.dumps(result))


if __name__ == '__main__':
    main()
