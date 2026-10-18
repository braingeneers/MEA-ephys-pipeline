#!/usr/bin/env python3
"""Prepare reproducible native-ADC Maxwell controls for full modeled writer checks.

Run offline in the pinned SI 0.103.2 codec-enabled base image. Files are created in
one fresh external directory. Ground truth is generated input, never a KS4 outcome.
The caller supplies complete modeled projection and recording templates explicitly.
"""
import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import inspect
import json
from pathlib import Path

import h5py
import numpy as np
import spikeinterface as si

RATE = 20000
DURATION = 180
CHANNELS = 32
UNITS = 20
SEED = 42
LSB_VOLTS = 6.3e-6
ADC_BASELINE = 512
START_TIME = '2026-01-01T00:00:00+00:00'
MAPPING_DTYPE = [('channel', '<i4'), ('electrode', '<i4'), ('x', '<f8'), ('y', '<f8')]


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    with path.open('x') as stream:
        stream.write(json.dumps(value, indent=2) + '\n')


def reference(path):
    digest = sha(path)
    return dict(uri=path.as_uri(), size=path.stat().st_size, sha256=digest, etag='sha256-' + digest)


def create_acquisition(handle, index, well, geometry):
    group = handle.create_group('data_store/data%04d' % index)
    group['settings/sampling'] = np.array([RATE], dtype='<i4')
    group['settings/lsb'] = np.array([LSB_VOLTS], dtype='<f8')
    group['settings/mapping'] = np.array(
        [(i, 100 + i, *xy) for i, xy in enumerate(geometry)], dtype=MAPPING_DTYPE)
    stamp = int(datetime.fromisoformat(START_TIME).timestamp() * 1000)
    group['start_time'] = np.array([stamp], dtype='<i8')
    group['stop_time'] = np.array([stamp + DURATION * 1000], dtype='<i8')
    group['groups/routed/channels'] = np.arange(CHANNELS, dtype='<u2')
    raw = group.create_dataset('groups/routed/raw', shape=(CHANNELS, RATE * DURATION),
                               dtype='<u2', chunks=(1, RATE), compression=401)
    handle.require_group('wells/' + well)['rec0000'] = group
    handle.require_group('recordings/rec0000')[well] = group
    return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--recordings-template', type=Path, required=True)
    parser.add_argument('--maxone-projection', type=Path, required=True)
    parser.add_argument('--maxtwo-projection', type=Path, required=True)
    args = parser.parse_args()
    if si.__version__ != '0.103.2':
        parser.error('Use the pinned SI 0.103.2 scientific image.')
    if not h5py.h5z.filter_avail(401):
        parser.error('The pinned Maxwell filter 401 must support input encoding.')
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    kwargs = dict(durations=[DURATION], num_channels=CHANNELS, num_units=UNITS,
                  sampling_frequency=RATE, seed=SEED)
    recording, sorting = si.generate_ground_truth_recording(**kwargs)
    assert recording.get_num_frames() == RATE * DURATION and recording.get_dtype() == np.dtype('float32')
    geometry = recording.get_channel_locations()
    assert geometry.shape == (CHANNELS, 2) and np.isfinite(geometry).all()
    sorting.save(folder=root / 'ground_truth')
    spike_vector = sorting.to_spike_vector()
    assert len(spike_vector) > 0 and (spike_vector['sample_index'] >= 0).all()
    assert (spike_vector['sample_index'] < RATE * DURATION).all()
    np.save(root / 'generated-spikes.npy', spike_vector, allow_pickle=False)
    np.save(root / 'geometry-um.npy', geometry, allow_pickle=False)
    template_records = json.loads(args.recordings_template.read_text())
    if isinstance(template_records, dict):
        template_records = template_records['recordings']
    by_case = {row['data_format']: deepcopy(row) for row in template_records}
    assert set(by_case) == {'maxone', 'maxtwo'}
    targets = []
    manifests = []
    generated_hash, adc_hash = hashlib.sha256(), hashlib.sha256()
    adc_min, adc_max, worst_error = 1024, -1, 0.0
    with ExitStack() as stack:
        generator = stack.enter_context((root / 'generator-traces.f32').open('xb'))
        encoded = stack.enter_context((root / 'encoded-adc.u16').open('xb'))
        for case, format_name, projection in (
                ('maxone', 'maxone', args.maxone_projection),
                ('maxtwo_routed', 'maxtwo', args.maxtwo_projection)):
            folder = root / case
            folder.mkdir()
            path = folder / 'recording.raw.h5'
            handle = stack.enter_context(h5py.File(path, 'x'))
            handle['version'] = np.array([b'20190530'])
            wells = ('well000',) if case == 'maxone' else ('well000', 'well002')
            for index, well in enumerate(wells):
                targets.append(create_acquisition(handle, index, well, geometry))
            model = json.loads(projection.read_text())
            old = dict(dataset_uuid=model['dataset_uuid'], recording_id=model['recording_id'],
                       session_id=model['context']['session_id'], duration_s=model['context']['recording_duration_s'])
            model['dataset_uuid'] = 'generated-ks4-native-control-' + case
            model['recording_id'] = 'native-control-' + case
            model['context']['session_id'] = 'native-control-' + case + '-seed42'
            model['context']['recording_duration_s'] = DURATION
            write_json(folder / 'projection.json', model)
            row = by_case[format_name]
            row['input_uri'] = str(path)
            unit = 'recording' if case == 'maxone' else 'well002'
            row['nwb_metadata'] = {unit: reference(folder / 'projection.json')}
            row['native_control'] = dict(case=case, generated=True, seed=SEED,
                                         source_projection_sha256=sha(projection))
            manifests.append(dict(case=case, path=str(path), selected_well=wells[-1],
                                  selected_raw_path='/data_store/data%04d/groups/routed/raw' % (len(wells) - 1),
                                  projection_template_sha256=sha(projection), original_projection_identity=old,
                                  recordings_entry=row))
        for start in range(0, RATE * DURATION, RATE):
            stop = min(start + RATE, RATE * DURATION)
            traces = np.asarray(recording.get_traces(start_frame=start, end_frame=stop), dtype='<f4')
            assert traces.shape == (stop - start, CHANNELS) and np.isfinite(traces).all()
            values = np.rint(traces.astype(np.float64) / (LSB_VOLTS * 1e6) + ADC_BASELINE)
            assert values.min() >= 0 and values.max() <= 1023, 'ADC range exceeded; never clip a control.'
            adc = values.astype('<u2')
            adc_min = min(adc_min, int(adc.min()))
            adc_max = max(adc_max, int(adc.max()))
            worst_error = max(worst_error, float(np.max(np.abs(
                (adc.astype(np.float64) - ADC_BASELINE) * LSB_VOLTS * 1e6 - traces))))
            source_bytes, adc_bytes = traces.tobytes(), adc.tobytes()
            generator.write(source_bytes)
            encoded.write(adc_bytes)
            generated_hash.update(source_bytes)
            adc_hash.update(adc_bytes)
            for dataset in targets:
                dataset[:, start:stop] = adc.T
    assert worst_error <= LSB_VOLTS * 1e6 / 2 + 1e-12
    assert sha(root / 'generator-traces.f32') == generated_hash.hexdigest()
    assert sha(root / 'encoded-adc.u16') == adc_hash.hexdigest()
    for row in manifests:
        row['source'] = reference(Path(row['path']))
        row['projection'] = reference(Path(row['path']).parent / 'projection.json')
    recipe = dict(schema_version=1, generated=True, observed_sorter_outcome=False,
                  call='spikeinterface.generate_ground_truth_recording', kwargs=kwargs,
                  generator_source_sha256=hashlib.sha256(inspect.getsource(si.generate_ground_truth_recording).encode()).hexdigest(),
                  generator_signature=str(inspect.signature(si.generate_ground_truth_recording)),
                  packages={name: importlib.metadata.version(name) for name in ('spikeinterface', 'numpy', 'scipy', 'h5py', 'probeinterface')},
                  generator_signal=dict(path='generator-traces.f32', dtype='<f4', shape=[RATE * DURATION, CHANNELS],
                                        sha256=generated_hash.hexdigest(), physical_unit='uV'),
                  native_signal=dict(path='encoded-adc.u16', dtype='<u2', shape=[RATE * DURATION, CHANNELS],
                                     sha256=adc_hash.hexdigest(), sampling_frequency=RATE, conversion_volts=LSB_VOLTS,
                                     offset_volts=0.0, baseline_adc=ADC_BASELINE, min_adc=adc_min, max_adc=adc_max,
                                     quantizer='rint(float64(generator_uV)/(lsb_volts*1e6)+baseline_adc)',
                                     clipped_values=0, max_baseline_relative_quantization_error_uV=worst_error),
                  ground_truth=dict(path='ground_truth', units=sorting.unit_ids.tolist(), spikes=len(spike_vector),
                                    spike_vector_sha256=sha(root / 'generated-spikes.npy'), observed_ks4=False),
                  geometry=dict(path='geometry-um.npy', unit='um', sha256=sha(root / 'geometry-um.npy')),
                  session_start_time=START_TIME, inputs=manifests)
    write_json(root / 'generation.json', recipe)
    write_json(root / 'recordings.json', [row['recordings_entry'] for row in manifests])
    print(json.dumps(dict(passed=True, frames=RATE * DURATION, channels=CHANNELS,
                         source_raw_datasets=len(targets), generated_spikes=len(spike_vector),
                         adc_range=[adc_min, adc_max], quantization_error_uV=worst_error,
                         observed_ks4_outcomes=0), indent=2))


if __name__ == '__main__':
    main()
