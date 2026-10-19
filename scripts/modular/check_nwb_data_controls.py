#!/usr/bin/env python3
"""Fresh schema-valid corruption controls for input-derived NWB storage checks.

Use independently copied final Units stores with raw disabled/enabled, a compiled
input-only manifest and an empty evidence directory. Original stores are read-only.
Each mutation has its own physical copy; replaced array bytes are retained outside
that copy. This command validates the stored-data guard, not full writer acceptance.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import uuid

import numpy as np
import zarr
from hdmf_zarr import NWBZarrIO
from pynwb import validate

from nwb_data_guard import file_sha, numeric_sha, typed, verify_store


def inventory(root):
    return {str(p.relative_to(root)): file_sha(p) for p in root.rglob('*') if p.is_file()}


def fingerprint(array):
    result = dict(dtype=array.dtype.str, shape=list(array.shape), attrs=dict(array.attrs))
    if array.dtype.kind in 'biufc':
        result['payload_sha256'] = numeric_sha(array)
    else:
        result['values'] = array[:].tolist()
    return result


def replace_dtype(store, name, dtype, retained):
    array = store[name]
    data, attrs = array[:].astype(dtype), dict(array.attrs)
    compressor, chunks, filters = array.compressor, array.chunks, array.filters
    source = Path(store.store.path) / name
    # Keep the original copied payload rather than deleting it or touching inputs.
    shutil.move(str(source), str(retained))
    parent, leaf = name.rsplit('/', 1)
    new = store[parent].create_dataset(leaf, data=data, chunks=chunks,
                                       compressor=compressor, filters=filters)
    attrs['zarr_dtype'] = np.dtype(dtype).name
    new.attrs.update(attrs)


def mutate(store, name, operation, retained):
    array = store[name]
    if operation == 'tail':
        old_length = len(array)
        array.resize(old_length + 1)
        array[old_length] = 0.125 if array.dtype.kind == 'f' else 0
    elif operation == 'index-last':
        array[-1] = int(array[-1]) - 1
    elif operation == 'index-boundary':
        array[0] = int(array[0]) + 1
    elif operation == 'value':
        array[0] = int(array[0]) + 1
    elif operation.startswith('dtype:'):
        replace_dtype(store, name, operation.split(':', 1)[1], retained)
    elif operation.startswith('binding:'):
        field = operation.split(':', 1)[1]
        binding = dict(array.attrs[field])
        binding['value'] = dict(binding['value'], source_object_id=str(uuid.uuid4()))
        array.attrs[field] = binding
    else:
        raise ValueError(operation)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nwb', type=Path, required=True)
    parser.add_argument('--raw-nwb', type=Path, required=True)
    parser.add_argument('--expected', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(root / 'capsules/nwb_common/code'),
                    str(root / 'pipeline/components/maxwell/runtime')]
    from source_nwb import open_source_nwb
    args.output.mkdir(exist_ok=False)
    expected = json.loads(args.expected.read_text())
    originals = {False: args.nwb, True: args.raw_nwb}
    original_hashes = {raw: inventory(path) for raw, path in originals.items()}
    lfp_region = next(name for name in expected['neural']['lfp'] if name.endswith('/electrodes'))
    raw_region = next(name for name in expected['neural']['raw'] if name.endswith('/electrodes'))
    electrode_source = 'general/extracellular_ephys/electrodes/electrode'
    controls = [
        ('spike-tail', 'units/spike_times', 'tail', False, 'complete stored shape'),
        ('spike-target-value', 'units/spike_times', 'value', False, 'complete stored values'),
        ('spike-index-last', 'units/spike_times_index', 'index-last', False, 'complete stored values'),
        ('spike-index-boundary', 'units/spike_times_index', 'index-boundary', False, 'complete stored values'),
        ('spike-index-dtype', 'units/spike_times_index', 'dtype:uint32', False, 'stored dtype'),
        ('spike-target-dtype', 'units/spike_times', 'dtype:float32', False, 'stored dtype'),
        ('electrode-tail', 'units/electrodes', 'tail', False, 'complete stored shape'),
        ('electrode-index-last', 'units/electrodes_index', 'index-last', False, 'complete stored values'),
        ('electrode-index-boundary', 'units/electrodes_index', 'index-boundary', False, 'complete stored values'),
        ('electrode-index-dtype', 'units/electrodes_index', 'dtype:uint32', False, 'stored dtype'),
        ('electrode-target-dtype', 'units/electrodes', 'dtype:int32', False, 'stored dtype'),
        ('electrode-target-value', 'units/electrodes', 'value', False, 'complete stored values'),
        ('lfp-region-value', lfp_region, 'value', False, 'complete stored values'),
        ('lfp-region-dtype', lfp_region, 'dtype:int32', False, 'stored dtype'),
        ('raw-region-value', raw_region, 'value', True, 'complete stored values'),
        ('raw-region-dtype', raw_region, 'dtype:int32', True, 'stored dtype'),
        ('spike-index-reference', 'units/spike_times_index', 'binding:target', False, 'stored target binding'),
        ('electrode-index-reference', 'units/electrodes_index', 'binding:target', False, 'stored target binding'),
        ('lfp-table-reference', lfp_region, 'binding:table', False, 'stored table binding'),
        ('raw-table-reference', raw_region, 'binding:table', True, 'stored table binding'),
        ('source-electrode-dtype', electrode_source, 'dtype:int64', False, 'stored dtype'),
        ('boolean-property-dtype', 'units/default_qc', 'dtype:int8', False, 'stored dtype'),
    ]
    results = []
    for raw, original in originals.items():
        target = args.output / ('unchanged-raw' if raw else 'unchanged-lfp')
        shutil.copytree(original, target)
        assert inventory(target) == original_hashes[raw]
        with open_source_nwb(target, 'r', NWBZarrIO, for_writing=True) as io:
            nwb = io.read()
            errors = validate(io=io)
            assert not errors, errors
            verify_store(target, nwb, expected, 'nwb_units', raw)
        results.append(dict(label=target.name, unchanged_copy_pass=True))
    for label, name, operation, raw, failure in controls:
        target = args.output / label
        shutil.copytree(originals[raw], target)
        store = zarr.open_group(str(target), mode='a')
        before = fingerprint(store[name])
        mutate(store, name, operation, args.output / (label + '-original-array'))
        after = fingerprint(store[name])
        assert typed(before) != typed(after), label + ': target did not change'
        zarr.consolidate_metadata(str(target))
        with open_source_nwb(target, 'r', NWBZarrIO, for_writing=True) as io:
            nwb = io.read()
            errors = validate(io=io)
            if label == 'spike-target-dtype':
                # The pinned Units schema requires float64. Preserve this as a
                # separate schema-invalid control, never scientific guard proof.
                assert errors and all('float64' in str(e) and 'float32' in str(e) for e in errors), (label, errors)
            else:
                assert not errors, (label, errors)
            try:
                verify_store(target, nwb, expected, 'nwb_units', raw)
            except AssertionError as error:
                message = str(error)
                assert name + ': ' + failure in message, (label, message)
            else:
                raise AssertionError(label + ': corrupted payload accepted')
        row = dict(label=label, path=name, operation=operation, raw=raw, schema_valid=not errors,
                   schema_errors=[str(e) for e in errors],
                   changed_target=True, intended_scientific_rejection=message, before=before, after=after)
        (args.output / (label + '.json')).write_text(json.dumps(row, indent=2) + '\n')
        results.append(row)
    # Reader-unmodeled data must also fail. Keep this separate from typed columns.
    target = args.output / 'unaccounted-dataset'
    shutil.copytree(originals[False], target)
    extra = str(Path(lfp_region).parent / 'unaccounted')
    zarr.open_group(str(target), mode='a').create_dataset(extra, data=np.asarray([1], dtype=np.int64))
    zarr.consolidate_metadata(str(target))
    with open_source_nwb(target, 'r', NWBZarrIO, for_writing=True) as io:
        nwb = io.read()
        errors = validate(io=io)
        assert not errors, errors
        try:
            verify_store(target, nwb, expected, 'nwb_units', False)
        except AssertionError as error:
            message = str(error)
            assert 'complete stored dataset inventory' in message and extra in message, message
        else:
            raise AssertionError('unaccounted stored dataset accepted')
    results.append(dict(label='unaccounted-dataset', path=extra, schema_valid=True,
                        changed_target=True, intended_scientific_rejection=message))
    assert all(inventory(path) == original_hashes[raw] for raw, path in originals.items())
    valid_controls = sum(c.get('schema_valid', False) for c in results)
    record = dict(passed=True, unchanged_copy_controls=2, schema_valid_scientific_controls=valid_controls,
                  schema_invalid_controls=1,
                  controls=results, original_inputs_unchanged=True, complete_parent_acceptance=False)
    with (args.output / 'result.json').open('x') as stream:
        stream.write(json.dumps(record, indent=2) + '\n')
    print(json.dumps(dict(passed=True, unchanged_copies=2, schema_valid_scientific_mutations_rejected=valid_controls,
                          schema_invalid_controls=1)))


if __name__ == '__main__':
    main()
