"""Test-only selected MaxTwo data in MaxOne topology for the unchanged baseline.

This does not add production MaxTwo support or rewrite original inputs/projections.
Expectations come from the original raw graph and independent schema-2 input facts.
"""
import argparse
import hashlib
import inspect
import json
from pathlib import Path
import sys

import h5py
import numpy as np


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def configure():
    directory = (Path('/opt/workflow/scripts') if Path('/opt/workflow/scripts').exists()
                 else Path('/opt/ephys-modular/maxwell/scripts'))
    sys.path.insert(0, str(directory))
    from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
    return configure_bundled_maxwell_hdf5_plugin()


def facts_for(inputs):
    folder = Path(inputs) / 'maxtwo_routed'
    facts = json.loads((folder / 'facts.json').read_text())
    require(facts['schema_version'] == 2, 'schema-2 source facts')
    require(sha(folder / 'recording.raw.h5') == facts['source']['sha256'], 'original source identity')
    require(sha(folder / 'projection.json') == facts['projection']['sha256'], 'authored projection identity')
    selected, = [row for row in facts['stores'] if row['selected']]
    require(selected['well'] == 'well002' and selected['store'] == '/data_store/data0001', 'nontrivial original route')
    return folder, facts, selected


def build(inputs, output):
    configure()
    folder, facts, selected = facts_for(inputs)
    output = Path(output)
    require(not output.exists(), 'reference output must be new')
    output.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(folder / 'recording.raw.h5', 'r') as source, h5py.File(output, 'x') as target:
        for key, value in source.attrs.items():
            target.attrs[key] = value
        for name in source:
            if name not in ('data_store', 'wells', 'recordings'):
                source.copy(name, target, name=name)
        stores = target.create_group('data_store')
        for key, value in source['data_store'].attrs.items():
            stores.attrs[key] = value
        source.copy(selected['store'], stores, name='data0000')
        store = stores['data0000']
        wells = target.create_group('wells')
        well = wells.create_group('well000')
        recordings = target.create_group('recordings')
        recording = recordings.create_group('rec0000')
        for original, copied in [(source['wells'], wells), (source['wells/well002'], well),
                                 (source['recordings'], recordings), (source['recordings/rec0000'], recording)]:
            for key, value in original.attrs.items():
                copied.attrs[key] = value
        well['rec0000'] = store
        recording['well000'] = store
    return check(inputs, output)


def payload(left, right, label):
    left, right = np.asarray(left), np.asarray(right)
    require(left.dtype == right.dtype and left.shape == right.shape, label + ': native dtype/shape')
    if left.dtype.hasobject:
        require(h5py.check_string_dtype(left.dtype) is not None, label + ': unsupported reference payload')
        require(all(type(a) is type(b) and a == b for a, b in zip(left.flat, right.flat)), label + ': values')
    else:
        require(left.tobytes() == right.tobytes(), label + ': complete payload')


def check(inputs, view, *, baseline_reader=False):
    plugin = configure()
    folder, facts, selected = facts_for(inputs)
    expected = np.load(folder / selected['signal']['file'], allow_pickle=False)
    clock = np.load(folder / selected['clock']['file'], allow_pickle=False)
    mapping = np.load(folder / selected['mapping']['file'], allow_pickle=False)
    pairs, reverse = {}, {}
    counts = dict(physical_objects=0, metadata_datasets=0, attributes=0, aliases=0, raw_chunks=0)

    def attrs(left, right, name):
        require(set(left.attrs) == set(right.attrs), name + ': complete attributes')
        for key in left.attrs:
            payload(left.attrs[key], right.attrs[key], name + '@' + key)
            counts['attributes'] += 1

    def graph(left, right, name):
        require(type(left) is type(right), name + ': object kind')
        if left.id in pairs:
            require(right.id == pairs[left.id], name + ': source alias identity')
            counts['aliases'] += 1
            return
        require(right.id not in reverse, name + ': distinct metadata objects collapsed')
        pairs[left.id], reverse[right.id] = right.id, left.id
        counts['physical_objects'] += 1
        attrs(left, right, name)
        if isinstance(left, h5py.Group):
            require(set(left) == set(right), name + ': complete children')
            for key in left:
                require(isinstance(left.get(key, getlink=True), h5py.HardLink), name + ': source dependency')
                require(isinstance(right.get(key, getlink=True), h5py.HardLink), name + ': view dependency')
                graph(left[key], right[key], name + '/' + key)
        else:
            payload(left[()], right[()], name)
            for property_name in ('chunks', 'maxshape', 'compression', 'compression_opts', 'shuffle', 'fletcher32', 'scaleoffset'):
                require(getattr(left, property_name) == getattr(right, property_name), name + ': ' + property_name)
            payload(left.fillvalue, right.fillvalue, name + ': fill value')
            a, b = left.id.get_create_plist(), right.id.get_create_plist()
            require(a.get_nfilters() == b.get_nfilters(), name + ': filter count')
            require([a.get_filter(i) for i in range(a.get_nfilters())] ==
                    [b.get_filter(i) for i in range(b.get_nfilters())], name + ': filter pipeline')
            counts['metadata_datasets'] += 1

    with h5py.File(folder / 'recording.raw.h5', 'r') as source, h5py.File(view, 'r') as target:
        attrs(source, target, '/')
        require(set(source['data_store']) == {'data0000', 'data0001'}, 'original stores retained')
        other_root = set(source) - {'data_store', 'wells', 'recordings'}
        require(set(target) == other_root | {'data_store', 'wells', 'recordings'}, 'reference root children')
        for name in other_root:
            graph(source[name], target[name], '/' + name)
        require(set(target['data_store']) == {'data0000'}, 'one explicitly selected view store')
        attrs(source['data_store'], target['data_store'], '/data_store')
        graph(source[selected['store']], target['data_store/data0000'], 'selected-store')
        for original, copied, name in [('wells', 'wells', '/wells'), ('wells/well002', 'wells/well000', 'selected-well'),
                                        ('recordings', 'recordings', '/recordings'),
                                        ('recordings/rec0000', 'recordings/rec0000', 'selected-recording')]:
            attrs(source[original], target[copied], name)
        require(set(target['wells']) == {'well000'} and set(target['wells/well000']) == {'rec0000'}, 'view well routing')
        require(set(target['recordings']) == {'rec0000'} and set(target['recordings/rec0000']) == {'well000'}, 'view recording routing')
        require(source['wells/well002/rec0000'].id == source[selected['store']].id == source['recordings/rec0000/well002'].id,
                'original selected alias routing')
        require(target['wells/well000/rec0000'].id == target['data_store/data0000'].id == target['recordings/rec0000/well000'].id,
                'selected view alias routing')
        counts['aliases'] += 2
        raw = target['data_store/data0000/groups/routed/raw']
        payload(expected, raw[()].T, 'independent selected signal')
        payload(mapping, target['data_store/data0000/settings/mapping'][()], 'independent selected mapping/geometry')
        require(float(target['data_store/data0000/settings/lsb'][0]).hex() == selected['source_lsb_hex'], 'independent calibration')
        require(target['data_store/data0000/settings/sampling'][0] == selected['rate_hz'], 'independent sampling rate')
        require(raw.id.get_num_chunks() == len(selected['source_chunks']), 'complete selected chunk inventory')
        for chunk in selected['source_chunks']:
            mask, data = raw.id.read_direct_chunk(tuple(chunk['coordinate']))
            require(mask == chunk['filter_mask'] and len(data) == chunk['bytes'] and hashlib.sha256(data).hexdigest() == chunk['sha256'],
                    'exact selected encoded chunk/mask')
            counts['raw_chunks'] += 1
    reader = None
    if baseline_reader:
        from convert_maxone_to_nwb import load_maxone_interface_class
        interface_class = load_maxone_interface_class()
        signature = inspect.signature(interface_class)
        kwargs = dict(file_path=str(view))
        for key, value in [('hdf5_plugin_path', plugin), ('download_plugin', False), ('verbose', False)]:
            if key in signature.parameters:
                kwargs[key] = value
        interface = interface_class(**kwargs)
        recording = interface.recording_extractor
        require(recording.get_num_segments() == 1, 'baseline reader segment count')
        payload(expected, recording.get_traces(), 'baseline reader every selected sample')
        payload(clock, recording.get_times(), 'baseline reader complete acquisition clock')
        require(recording.get_sampling_frequency() == selected['rate_hz'], 'baseline reader sampling rate')
        require(all(float(v).hex() == selected['gain_microvolts_hex'] for v in recording.get_channel_gains()), 'baseline reader every channel gain')
        require(all(float(v).hex() == selected['offset_hex'] for v in recording.get_channel_offsets()), 'baseline reader every channel offset')
        payload(np.column_stack([mapping['x'], mapping['y']]), recording.get_channel_locations(), 'baseline reader electrode geometry')
        require([str(v) for v in recording.get_channel_ids()] == [str(v) for v in mapping['channel']], 'baseline reader channel mapping')
        reader = dict(interface=interface_class.__module__ + '.' + interface_class.__name__, samples_checked=int(expected.size),
                      time_values_checked=int(clock.size), gain_offset_channels=expected.shape[1], geometry_values=expected.shape[1] * 2)
    return dict(passed=True, compatibility_label='Selected MaxTwo well002/data0001 represented in MaxOne well000/data0000 topology',
                original_source_sha256=facts['source']['sha256'], original_projection_sha256=facts['projection']['sha256'],
                view_sha256=sha(view), original_projection_modified=False, original_source_modified=False,
                original_max_two_support_claimed=False, selected_raw_original=selected['raw_dataset_path'],
                selected_raw_view='/data_store/data0000/groups/routed/raw', original_well='well002', view_well='well000',
                omitted_unselected_store='/data_store/data0000', retained_metadata=counts, baseline_reader=reader,
                original_facts_schema=2, output_oracle_acceptance=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('build', 'check'))
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--view', type=Path, required=True)
    parser.add_argument('--baseline-reader', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    require(not args.report.exists(), 'proof report must be new')
    require(not (args.operation == 'build' and args.baseline_reader),
            'Build first, then check with --baseline-reader in the original runtime')
    result = build(args.inputs, args.view) if args.operation == 'build' else check(args.inputs, args.view, baseline_reader=args.baseline_reader)
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
