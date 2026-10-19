#!/usr/bin/env python3
"""Fresh metadata mutations: declared variable fields vs fixed-schema violations."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import sys
import uuid

import zarr
from hdmf_zarr import NWBZarrIO
from pynwb import validate

from nwb_data_guard import file_sha, require, typed, verify_store as verify_data
from nwb_metadata_guard import snapshot, verify_store


def inventory(path):
    return {str(p.relative_to(path)): file_sha(p) for p in path.rglob('*') if p.is_file()}


def run_controls(source, metadata, data, role, raw, output, open_source_nwb):
    """Originals stay read-only; mutation bytes and every outcome remain available."""
    require(role == 'nwb_units' and metadata['profiles'], 'positive final Units fixture required')
    before = inventory(source)
    with open_source_nwb(source, 'r', NWBZarrIO, for_writing=True) as io:
        nwb = io.read()
        require(nwb.units is not None, 'positive Units fixture')
        baseline = snapshot(source, nwb, metadata['source_ids'])
        catalog = io.manager.type_map.namespace_catalog
        units = catalog.get_spec('core', 'Units')
        electrical = catalog.get_spec('core', 'ElectricalSeries')
        rules = {
            'waveform-rate': units.get_dataset('waveform_mean').get_attribute('sampling_rate'),
            'spike-resolution': units.get_dataset('spike_times').get_attribute('resolution'),
            'lfp-resolution': electrical.get_dataset('data').get_attribute('resolution'),
            'lfp-conversion': electrical.get_dataset('data').get_attribute('conversion'),
            'series-filtering': electrical.get_attribute('filtering'),
            'missing-series-description': electrical.get_attribute('description'),
            'units-description': units.get_attribute('description'),
            'column-description': catalog.get_spec('hdmf-common', 'VectorData').get_attribute('description'),
            'module-description': catalog.get_spec('core', 'ProcessingModule').get_attribute('description'),
            'device-binding': catalog.get_spec('core', 'ElectrodeGroup').get_link('device'),
            'waveform-unit': units.get_dataset('waveform_mean').get_attribute('unit'),
            'clock-interval': electrical.get_dataset('timestamps').get_attribute('interval'),
        }
        require(all(value is not None for value in rules.values()), 'all mutation schema rules resolved')
        for name, spec in rules.items():
            if name not in ('waveform-unit', 'clock-interval', 'device-binding'):
                require('value' not in spec, name + ': genuinely variable declared metadata')
        require(not rules['missing-series-description'].required, 'description removal is schema-conforming')
        require(rules['waveform-unit']['value'] == 'volts' and rules['clock-interval']['value'] == 1,
                'fixed schema constraints retained separately')
        group = next(iter(nwb.electrode_groups.values()))
        group_path = next(path for path, node in baseline['model'].items()
                          if node['name'] == group.name and node['neurodata_type'] == 'ElectrodeGroup')
    lfp = next(path.removesuffix('/.zattrs') for path, attrs in baseline['physical_metadata'].items()
               if attrs.get('neurodata_type') == 'ElectricalSeries' and path.startswith('processing/'))
    controls = ['unchanged', 'column-order', 'waveform-rate', 'spike-resolution', 'lfp-resolution',
                'lfp-conversion', 'series-filtering', 'missing-series-description', 'units-description',
                'column-description', 'module-description', 'extra-device', 'missing-device',
                'device-binding', 'waveform-unit', 'mixed-waveform-unit', 'clock-interval']
    results = []
    for name in controls:
        target = output / name
        shutil.copytree(source, target)
        store = zarr.open_group(str(target), mode='a')
        if name == 'column-order':
            names = list(store['units'].attrs['colnames'])
            store['units'].attrs['colnames'] = names[1:] + names[:1]
        elif name == 'waveform-rate':
            for column in ('waveform_mean', 'waveform_sd'):
                store['units/' + column].attrs['sampling_rate'] = 12345.0
        elif name == 'spike-resolution':
            store['units/spike_times'].attrs['resolution'] = 0.125
        elif name == 'lfp-resolution':
            store[lfp + '/data'].attrs['resolution'] = 0.5
        elif name == 'lfp-conversion':
            store[lfp + '/data'].attrs['conversion'] *= 2.0
        elif name == 'series-filtering':
            store[lfp].attrs['filtering'] = 'QA: additional bandpass filtering'
        elif name == 'missing-series-description':
            del store[lfp].attrs['description']
        elif name == 'units-description':
            store['units'].attrs['description'] = 'QA: different sorter provenance'
        elif name == 'column-description':
            store['units/firing_rate'].attrs['description'] = 'QA: changed rate interpretation'
        elif name == 'module-description':
            store['processing/ecephys'].attrs['description'] = 'QA: different processing interpretation'
        elif name == 'extra-device':
            store.create_group('general/devices/QAMetadataDevice').attrs.update(
                dict(description='QA extra device', namespace='core', neurodata_type='Device', object_id=str(uuid.uuid4())))
        elif name == 'missing-device':
            shutil.move(str(target / 'general/devices/Device'), str(output / 'removed-default-device'))
        elif name == 'device-binding':
            links = copy.deepcopy(store[group_path].attrs['zarr_link'])
            link = next(link for link in links if link['name'] == 'device')
            link['path'] = '/general/devices/Device'
            store[group_path].attrs['zarr_link'] = links
        elif name in ('waveform-unit', 'mixed-waveform-unit'):
            for column in (('waveform_mean', 'waveform_sd') if name == 'waveform-unit' else ('waveform_sd',)):
                store['units/' + column].attrs['unit'] = 'millivolts'
        elif name == 'clock-interval':
            store[lfp + '/timestamps'].attrs['interval'] = 2
        if name != 'unchanged':
            zarr.consolidate_metadata(str(target))
        after = inventory(target)
        changed = {key: dict(before=before.get(key), after=after.get(key)) for key in set(before) | set(after)
                   if before.get(key) != after.get(key)}
        require(all(key == '.zmetadata' or key.endswith(('/.zattrs', '/.zgroup')) for key in changed),
                name + ': only metadata bytes changed')
        positive = name in ('unchanged', 'column-order')
        fixed = name in ('waveform-unit', 'mixed-waveform-unit', 'clock-interval')
        with open_source_nwb(target, 'r', NWBZarrIO, for_writing=True) as io:
            nwb = io.read()
            errors = [str(error) for error in validate(io=io)]
            actual = snapshot(target, nwb, metadata['source_ids'])
            require(not errors, name + ': unexpected validator failure (not a guard rejection): ' + str(errors))
            data_report = verify_data(target, nwb, data, role, raw)
            if not positive:
                require(changed, name + ': actual encoded metadata change')
                if name not in ('mixed-waveform-unit', 'clock-interval'):
                    require(typed(actual['model']) != typed(baseline['model']), name + ': actual decoded metadata change')
                if name == 'mixed-waveform-unit':
                    require(nwb.units.waveform_unit == 'volts', 'mixed physical unit masked by parent getter')
                    require(typed(actual['model']) == typed(baseline['model']), 'mixed unit: logical graph remains unchanged')
            try:
                report = verify_store(target, nwb, metadata, role, raw)
            except AssertionError as error:
                require(not positive and ('physical_metadata' in str(error) or 'model' in str(error)),
                        name + ': intended metadata comparison, not unrelated failure: ' + str(error))
                rejection = str(error)
            else:
                require(positive, name + ': changed scientific metadata accepted')
                rejection = None
        rule_key = 'waveform-unit' if name == 'mixed-waveform-unit' else name
        result = dict(control=name, classification=('unchanged/order positive' if positive else
                      'fixed-schema violation accepted by validator' if fixed else 'schema-conforming metadata change'),
                      pinned_validator_errors=errors, changed_metadata_files=changed,
                      all_nonmetadata_bytes_unchanged=True, complete_data_guard_pass=True,
                      stored_datasets=data_report['stored_datasets'], intended_rejection=rejection,
                      actual_decoded_graph_changed=typed(actual['model']) != typed(baseline['model']),
                      applicable_schema_rule=dict(rules[rule_key]) if rule_key in rules else None)
        results.append(result)
        print(name, 'accepted positive' if positive else 'rejected: ' + rejection, flush=True)
    require(inventory(source) == before, 'original complete store unchanged')
    return dict(passed=True, original_unchanged=True, fixed_violations_not_counted_as_schema_conforming=True,
                positives=sum(row['intended_rejection'] is None for row in results),
                schema_conforming_negatives=sum(row['classification'] == 'schema-conforming metadata change' for row in results),
                fixed_schema_negatives=sum('fixed-schema' in row['classification'] for row in results), results=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--helpers', type=Path)
    parser.add_argument('--nwb', type=Path, required=True)
    parser.add_argument('--expected', type=Path, required=True)
    parser.add_argument('--expected-data', type=Path, required=True)
    parser.add_argument('--write-raw', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.helpers:
        sys.path.insert(0, str(args.helpers))
    else:
        root = Path(__file__).resolve().parents[2]
        sys.path[:0] = [str(root / 'capsules/nwb_common/code'),
                        str(root / 'pipeline/components/maxwell/runtime')]
    from source_nwb import open_source_nwb
    args.output.mkdir(parents=True, exist_ok=False)
    report = run_controls(args.nwb, json.loads(args.expected.read_text()),
                          json.loads(args.expected_data.read_text()), 'nwb_units', args.write_raw,
                          args.output, open_source_nwb)
    with (args.output / 'result.json').open('x') as stream:
        stream.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'results'}), flush=True)


if __name__ == '__main__':
    main()
