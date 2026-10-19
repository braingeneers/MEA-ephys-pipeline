#!/usr/bin/env python3
"""Input-only facts for the maintained 180-second native scientific controls.

Generate acquisitions with scripts/modular/prepare_native_controls.py first, using
complete authored projections and explicit session_start_time. This command checks
every raw value against its separately retained ADC input and derives expectations
without opening any converted NWB. Generated spikes remain input ground truth.
"""
import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path

import h5py
import numpy as np

from inputs import (SCHEMA, describe_array, electrode_contract, model_contract,
                    sha, typed, write)


def array_file(folder, name, source, chunk_frames=60000):
    path = folder / name
    result = np.lib.format.open_memmap(path, mode='w+', dtype=source.dtype,
                                       shape=source.shape)
    for start in range(0, len(source), chunk_frames):
        result[start:start + chunk_frames] = source[start:start + chunk_frames]
    result.flush()
    del result
    return dict(file=name, sha256=sha(path), dtype=source.dtype.str,
                dtype_fields=source.dtype.descr if source.dtype.names else None,
                shape=list(source.shape))


def prepare(controls, template, output):
    controls, template, output = controls.resolve(), template.resolve(), output.resolve()
    if output.exists():
        raise ValueError('Facts require a new output directory')
    generation = json.loads((controls / 'generation.json').read_text())
    native = generation['native_signal']
    if (native['shape'] != [3600000, 32] or native['dtype'] != '<u2' or
            native['sampling_frequency'] != 20000 or generation['observed_sorter_outcome'] is not False):
        raise ValueError('Expected the maintained 180-second native input recipe')
    for key in ('native_signal', 'generator_signal', 'geometry'):
        item = generation[key]
        if sha(controls / item['path']) != item['sha256']:
            raise ValueError('Changed generated reference: ' + key)
    count, channels = native['shape']
    adc = np.memmap(controls / native['path'], mode='r', dtype=native['dtype'], shape=(count, channels))
    geometry = np.load(controls / generation['geometry']['path'], allow_pickle=False)
    if geometry.shape != (channels, 2):
        raise ValueError('Invalid independent input geometry')
    output.mkdir(parents=True)
    records = json.loads((controls / 'recordings.json').read_text())
    bindings, summaries = [], []
    for entry in generation['inputs']:
        case = entry['case']
        folder = output / case
        folder.mkdir()
        source = controls / case / 'recording.raw.h5'
        projection = controls / case / 'projection.json'
        for path, key in ((source, 'source'), (projection, 'projection')):
            if sha(path) != entry[key]['sha256'] or path.stat().st_size != entry[key]['size']:
                raise ValueError('Changed generated input ' + str(path))
        model = json.loads(projection.read_text())
        if (model['context']['recording_duration_s'] != 180 or
                model['context']['session_start_time'] != generation['session_start_time']):
            raise ValueError('Authored clock/duration must match full acquisition')
        stores = []
        with h5py.File(source, 'r') as handle:
            wells = ['well000'] if case == 'maxone' else ['well000', 'well002']
            for index, well in enumerate(wells):
                group = handle[f'/data_store/data{index:04d}']
                if (handle[f'/wells/{well}/rec0000'].id != group.id or
                        handle[f'/recordings/rec0000/{well}'].id != group.id):
                    raise ValueError('Source routing alias differs')
                raw = group['groups/routed/raw']
                if raw.shape != (channels, count) or raw.dtype != adc.dtype:
                    raise ValueError('Full source signal dtype/shape differs')
                mapping = group['settings/mapping'][:]
                if (not np.array_equal(mapping['channel'], np.arange(channels)) or
                        not np.array_equal(mapping['electrode'], 100 + np.arange(channels)) or
                        not np.array_equal(mapping['x'], geometry[:, 0]) or
                        not np.array_equal(mapping['y'], geometry[:, 1])):
                    raise ValueError('Source mapping differs from independent generator')
                rate = group['settings/sampling'][0].item()
                lsb = group['settings/lsb'][0].item()
                stamp = int(datetime.fromisoformat(generation['session_start_time']).timestamp() * 1000)
                if (rate != native['sampling_frequency'] or lsb != native['conversion_volts'] or
                        group['start_time'][0] != stamp or group['stop_time'][0] != stamp + 180000):
                    raise ValueError('Source calibration/time differs')
                for start in range(0, count, 60000):
                    actual, expected = raw[:, start:start + 60000].T, adc[start:start + 60000]
                    if actual.dtype != expected.dtype or not np.array_equal(actual, expected):
                        raise ValueError('Source sample differs: ' + case + '/' + str(index) + '/' + str(start))
                prefix = 'store' + str(index)
                chunks = []
                for number in range(raw.id.get_num_chunks()):
                    info = raw.id.get_chunk_info(number)
                    mask, encoded = raw.id.read_direct_chunk(info.chunk_offset)
                    chunks.append(dict(coordinate=list(info.chunk_offset), offset=info.byte_offset,
                                       bytes=len(encoded), filter_mask=mask,
                                       sha256=hashlib.sha256(encoded).hexdigest()))
                pipeline = []
                for number in range(raw.id.get_create_plist().get_nfilters()):
                    ident, flags, client, name = raw.id.get_create_plist().get_filter(number)
                    pipeline.append(dict(id=ident, flags=flags, client_data=list(client), name=name.decode()))
                stores.append(dict(store=group.name, well=well, raw_dataset_path=raw.name,
                    selected=raw.name == entry['selected_raw_path'],
                    signal=array_file(folder, prefix + '-signal.npy', adc),
                    clock=describe_array(folder, prefix + '-clock.npy', np.arange(count, dtype='<f8') / rate),
                    mapping=describe_array(folder, prefix + '-mapping.npy', mapping),
                    source_lsb_hex=float(lsb).hex(), gain_microvolts_hex=float(lsb * 1e6).hex(),
                    expected_nwb_conversion_hex=float((lsb * 1e6) * 1e-6).hex(), offset_hex=0.0.hex(),
                    rate_hz=rate, electrodes=electrode_contract(folder, prefix, mapping),
                    input_chunks=list(raw.chunks), expected_nwb_dtype=adc.dtype.str,
                    expected_nwb_shape=[count, channels], coordinate_units='micrometers',
                    input_filter_pipeline=pipeline, source_chunks=chunks))
        # The small fixture declares interface/schema semantics; none of its raw
        # values, channel count, model or timing supplies the long expectations.
        interface = deepcopy(json.loads((template / case / 'facts.json').read_text())['expected_interface'])
        core = dict(identifier=model['dataset_uuid'] + ':' + model['experiment_name'] + ':' + model['unit'],
            session_id=model['context']['session_id'], session_description=model['context']['session_description'],
            session_start_time=model['context']['session_start_time'],
            experimenter=[row['name'] for row in model['people'].values()], **model['common'])
        facts = dict(schema_version=SCHEMA, case=case,
            source=dict(file=str(source), size=source.stat().st_size, sha256=sha(source)),
            projection=dict(file=str(projection), size=projection.stat().st_size, sha256=sha(projection),
                            original_typed=typed(model)), stores=stores, model=model_contract(model),
            expected_interface=interface, expected_core=typed(core))
        write(folder / 'facts.json', facts)
        record, = [row for row in records if row['native_control']['case'] == case]
        unit = 'recording' if case == 'maxone' else 'well002'
        bindings.append(dict(input_uri=record['input_uri'], unit_name=unit,
                             metadata=dict(unit_id=case, inventory='retain-' + case,
                                           unknown_typed=deepcopy(record['unknown_typed']))))
        summaries.append(dict(case=case, frames=count, channels=channels, duration_s=count / rate,
                              raw_datasets=len(stores), sample_values_checked=count * channels * len(stores),
                              input_chunks=sum(len(row['source_chunks']) for row in stores),
                              facts_sha256=sha(folder / 'facts.json'), source_sha256=sha(source),
                              projection_sha256=sha(projection), selected_raw_path=entry['selected_raw_path']))
    write(output / 'bindings.json', list(reversed(bindings)))
    write(output / 'recordings.json', records)
    write(output / 'transport-facts.json', dict(recordings=typed(records), bindings=typed(list(reversed(bindings)))))
    result = dict(passed=True, source='input-only ADC/generator/mapping/authored projection', cases=summaries,
                  generated_spikes_are_sorter_outcomes=False, produced_nwb_outputs_used=False,
                  unselected_long_store_signal='The maintained generator repeats the signal in each well; '
                      'the separate small oracle supplies different-store routing discrimination.')
    write(output / 'input-validation.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controls', type=Path, required=True)
    parser.add_argument('--template', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.controls, args.template, args.output), indent=2))
