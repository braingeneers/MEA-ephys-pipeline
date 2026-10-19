"""Input-derived, complete stored-data checks for the pinned NWB Zarr writers.

This QA module supplements schema/model and logical scientific comparisons. Compile
expectations with only original source HDF5, independently derived writer facts and
their numeric arrays visible. Verification needs no original inputs. Do not derive
expected indices, buffer sizes, dtypes or dataset membership from a produced store.

The pinned HDMF-Zarr 0.12 policy exports UTF-8/ASCII text through VLenUTF8/VLenBytes,
and object references through Pickle(protocol=5). Scalar text becomes a one-element
array. UUID4 unit names and new Data object IDs are measured, validated exceptions;
source IDs and reference targets remain exact. Numeric NaN payloads are canonicalized
on both sides; dtype, shape, NaN positions, signed zero and all other bits are exact.
"""
from pathlib import Path
import hashlib
import json
import uuid

import h5py
import numpy as np
import zarr
from hdmf.container import Data


def require(condition, label):
    if not condition:
        raise AssertionError(label)


def typed(value):
    """Avoid Python's bool/int and int/float equality at logical boundaries."""
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, dict):
        return ('dict', tuple(sorted((str(k), typed(v)) for k, v in value.items())))
    if isinstance(value, (list, tuple)):
        return ('sequence', tuple(typed(v) for v in value))
    if isinstance(value, float):
        return ('float', value.hex())
    return (type(value).__name__, value)


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def numeric_sha(array):
    digest = hashlib.sha256()
    chunks = range(0, array.shape[0], 60000) if array.ndim else [None]
    for start in chunks:
        values = np.asarray(array[()] if start is None else array[start:start + 60000])
        if values.dtype.kind in 'fc':
            values = values.copy()
            values[np.isnan(values)] = np.nan
        digest.update(values.tobytes(order='C'))
    return digest.hexdigest()


def numeric(array):
    return dict(kind='numeric', dtype=array.dtype.str, shape=list(array.shape),
                sha256=numeric_sha(array), filters=[])


def index_array(lengths):
    """HDMF VectorIndex starts uint8, promoting 8→16→32→64 from target length."""
    require(all(type(n) is int and n >= 0 for n in lengths), 'index lengths: exact nonnegative integers')
    total = sum(lengths)
    bits = 8
    while total > 2 ** bits - 1:
        bits *= 2
        require(bits <= 64, 'index target exceeds uint64')
    return np.cumsum(lengths, dtype=np.uint64).astype(np.dtype('uint' + str(bits)))


def reference(path, object_id, root_id):
    return dict(source='.', path='/' + path.lstrip('/'), object_id=object_id,
                source_object_id=root_id)


def text(value):
    return value.decode('utf-8') if isinstance(value, bytes) else str(value)


def compile_expected(source_hdf5, facts, array_root):
    """Return a JSON-safe manifest derived solely from the declared original inputs."""
    source_hdf5, array_root = Path(source_hdf5), Path(array_root)
    require(file_sha(source_hdf5) == facts['source']['source_sha256'], 'source HDF5 identity')
    root_ids = [k for k, v in facts['source']['header'].items() if v['neurodata_type'] == 'NWBFile']
    electrode_ids = [k for k, v in facts['source']['header'].items() if v['neurodata_type'] == 'ElectrodesTable']
    require(len(root_ids) == len(electrode_ids) == 1, 'source root/electrode identities')
    root_id, electrode_id = root_ids[0], electrode_ids[0]
    model, neural, units = {}, {}, {}
    with h5py.File(source_hdf5, 'r') as source:
        require(text(source.attrs['object_id']) == root_id, 'source root UUID binding')

        def visit(name, obj):
            if not isinstance(obj, h5py.Dataset) or name.split('/')[0] in ('acquisition', 'processing', 'units', 'specifications'):
                return
            values = np.asarray(obj[()])
            if h5py.check_dtype(ref=obj.dtype) is not None:
                require(values.ndim == 1, name + ': supported source reference shape')
                refs = [reference(source[v].name, text(source[v].attrs['object_id']), root_id) for v in values]
                spec = dict(kind='references', dtype='|O', shape=list(values.shape), values=refs,
                            filters=[dict(id='pickle', protocol=5)])
            elif (string := h5py.check_string_dtype(obj.dtype)) is not None:
                values = np.atleast_1d(values)
                kind = 'utf8' if string.encoding == 'utf-8' else 'bytes'
                payload = values.tolist()
                if kind == 'utf8':
                    payload = [text(v) for v in payload]
                else:
                    payload = [bytes(v).hex() for v in payload]
                spec = dict(kind=kind, dtype='|O', shape=list(values.shape), values=payload,
                            filters=[dict(id='vlen-utf8' if kind == 'utf8' else 'vlen-bytes')])
            else:
                require(values.dtype.kind in 'biufc', name + ': unsupported source dtype')
                spec = numeric(values)
            if 'neurodata_type' in obj.attrs:
                spec['Data_type'] = text(obj.attrs['neurodata_type'])
                spec['object_id'] = text(obj.attrs['object_id'])
                spec['namespace'] = text(obj.attrs['namespace'])
            model[name] = spec

        source.visititems(visit)

    def load(descriptor):
        require(descriptor['kind'] == 'numeric', 'numeric input fact required')
        path = array_root / descriptor['file']
        require(file_sha(path) == descriptor['sha256'], 'expected array file identity')
        array = np.load(path, mmap_mode='r', allow_pickle=False)
        require(array.dtype.str == descriptor['dtype'] and list(array.shape) == descriptor['shape'], 'expected array dtype/shape')
        return array

    device = facts['source']['unit_device_name']
    table_path = 'general/extracellular_ephys/electrodes'
    for kind in ('lfp', 'raw'):
        f = facts['neural'][kind]
        prefix = ('processing/ecephys/LFP/ElectricalSeries' + device + '-LFP' if kind == 'lfp'
                  else 'acquisition/ElectricalSeries' + device)
        rows = numeric(np.asarray(f['electrode_rows'], dtype=np.int64))
        rows.update(Data_type='DynamicTableRegion', namespace='hdmf-common',
                    bindings=dict(table=reference(table_path, electrode_id, root_id)))
        neural[kind] = {prefix + '/data': numeric(load(f['data'])),
                        prefix + '/timestamps': numeric(load(f['times'])), prefix + '/electrodes': rows}
    u = facts['units']
    if u['units']:
        trains = [load(d) for d in u['spike_times']]
        require(len(trains) == u['units'] and sum(len(t) for t in trains) == u['spikes'], 'complete input spike count')
        units['units/spike_times'] = numeric(np.concatenate(trains))
        units['units/spike_times_index'] = numeric(index_array([len(t) for t in trains]))
        electrodes = np.asarray(u['electrode_rows'], dtype=np.int64)
        units['units/electrodes'] = numeric(np.tile(electrodes, u['units']))
        units['units/electrodes_index'] = numeric(index_array([len(electrodes)] * u['units']))
        units['units/id'] = numeric(load(u['unit_table_ids']))
        for name, descriptor in u['properties'].items():
            if descriptor['kind'] == 'numeric':
                units['units/' + name] = numeric(load(descriptor))
            else:
                require(np.dtype(descriptor['input_dtype']).kind == 'U', 'explicit unit text transform')
                units['units/' + name] = dict(kind='utf8', dtype='|O', shape=descriptor['shape'],
                                             values=descriptor['values'], filters=[dict(id='vlen-utf8')])
        for name in ('waveform_mean', 'waveform_sd'):
            units['units/' + name] = numeric(load(u[name]))
        units['units/unit_name'] = dict(kind='uuid4', dtype='|O', shape=[u['units']], filters=[dict(id='vlen-utf8')])
        for path, spec in units.items():
            name = path.split('/')[-1]
            spec.update(Data_type='VectorData', namespace='hdmf-common')
            if name == 'id':
                spec['Data_type'] = 'ElementIdentifiers'
            elif name.endswith('_index') and name in ('spike_times_index', 'electrodes_index'):
                spec.update(Data_type='VectorIndex', bindings=dict(target=reference('units/' + name.removesuffix('_index'), None, root_id)))
            elif name == 'electrodes':
                spec.update(Data_type='DynamicTableRegion', bindings=dict(table=reference(table_path, electrode_id, root_id)))
        require(set(p.removeprefix('units/') for p in units) == set(u['expected_columns']) | {'id', 'spike_times_index', 'electrodes_index'}, 'complete derived Units dataset membership')
    return dict(schema_version=1, source_sha256=facts['source']['source_sha256'], root_id=root_id,
                source_model=model, neural=neural, units=units, source_ids=list(facts['source']['header']),
                expected_processing=['ecephys'], expected_interfaces=['LFP'],
                schema_cache_path='specifications')


def verify_store(path, nwb, expected, role, write_raw):
    """Check all physical datasets and decoded Data bindings, including unused tails."""
    path = Path(path)
    require(role in ('nwb_ecephys', 'nwb_units') and type(write_raw) is bool, 'writer role/raw setting')
    require(expected['schema_version'] == 1 and nwb.object_id == expected['root_id'], 'manifest/root binding')
    specs = dict(expected['source_model'])
    specs.update(expected['neural']['lfp'])
    if write_raw:
        specs.update(expected['neural']['raw'])
    if role == 'nwb_units':
        specs.update(expected['units'])
    physical_metadata = {str(p.relative_to(path)): json.loads(p.read_text()) for p in path.rglob('*')
                         if p.name in ('.zarray', '.zattrs', '.zgroup')}
    consolidated = json.loads((path / '.zmetadata').read_text())['metadata']
    require(typed(physical_metadata) == typed(consolidated), 'complete consolidated/physical metadata inventory')
    store = zarr.open_group(str(path), mode='r')
    cache = expected['schema_cache_path']
    require(store.attrs['.specloc'] == cache, 'declared pinned schema cache path')
    actual_paths = {name.removesuffix('/.zarray') for name in physical_metadata
                    if name.endswith('/.zarray') and not name.startswith(cache + '/')}
    require(actual_paths == set(specs), 'complete stored dataset inventory: missing=' + str(sorted(set(specs) - actual_paths)) + ' extra=' + str(sorted(actual_paths - set(specs))))
    require(set(nwb.processing) == set(expected['expected_processing']), 'processing modules')
    require(set(nwb.processing['ecephys'].data_interfaces) == set(expected['expected_interfaces']), 'processing interface inventory')
    coverage, generated, data_ids = [], set(), set()
    for name, spec in sorted(specs.items()):
        array = store[name]
        require(typed(physical_metadata[name + '/.zarray']['shape']) == typed(spec['shape'])
                and typed(list(array.shape)) == typed(spec['shape']), name + ': complete stored shape')
        require(array.dtype.str == spec['dtype'], name + ': stored dtype')
        filters = [f.get_config() for f in (array.filters or [])]
        require(typed(filters) == typed(spec['filters']), name + ': stored codec/type')
        kind = spec['kind']
        if kind == 'numeric':
            require(numeric_sha(array) == spec['sha256'], name + ': complete stored values')
        else:
            values = array[:].tolist()
            if kind == 'bytes':
                require(all(type(v) is bytes for v in values), name + ': stored byte type')
                values = [v.hex() for v in values]
            elif kind in ('utf8', 'uuid4'):
                require(all(type(v) is str for v in values), name + ': stored text type')
            if kind == 'uuid4':
                require(len(set(values)) == len(values), name + ': unique generated names')
                for value in values:
                    require(uuid.UUID(value).version == 4 and value not in expected['source_ids'], name + ': generated UUID4')
                    require(value not in generated, name + ': unique generated UUID')
                    generated.add(value)
            else:
                require(typed(values) == typed(spec['values']), name + ': complete stored values/references')
        if 'Data_type' in spec:
            identifier = array.attrs['object_id']
            require(array.attrs['neurodata_type'] == spec['Data_type'] and array.attrs['namespace'] == spec['namespace'], name + ': Data type')
            require(identifier not in data_ids, name + ': duplicate Data object ID')
            data_ids.add(identifier)
            if 'object_id' in spec:
                require(identifier == spec['object_id'], name + ': source Data object ID')
            else:
                require(uuid.UUID(identifier).version == 4 and identifier not in expected['source_ids'], name + ': generated Data UUID4')
            obj = nwb.objects[identifier]
            require(isinstance(obj, Data), name + ': decoded Data binding')
            data = getattr(obj.data, 'dataset', obj.data)
            require(data.path == name, name + ': decoded Data storage path')
            for field, target in spec.get('bindings', {}).items():
                require(typed(dict(array.attrs[field])) == typed(dict(value=target, zarr_dtype='object')), name + ': stored ' + field + ' binding')
                target_id = store[target['path'].lstrip('/')].attrs['object_id']
                require(getattr(obj, field) is nwb.objects[target_id], name + ': decoded ' + field + ' binding')
        coverage.append(dict(path=name, kind=kind, dtype=spec['dtype'], shape=spec['shape'],
                             complete_payload=True, Data_type=spec.get('Data_type')))
    actual_data_ids = {identifier for identifier, obj in nwb.objects.items() if isinstance(obj, Data)}
    require(data_ids == actual_data_ids, 'complete decoded Data object inventory')
    return dict(stored_dataset_inventory_complete=True, decoded_Data_inventory_complete=True,
                stored_datasets=len(coverage), Data_objects=len(data_ids), coverage=coverage,
                reference_policy='exact same-store canonical path/source root and declared target',
                numeric_policy='exact dtype/shape/all bits except canonical NaN payloads',
                index_policy='input-derived cumulative lengths; HDMF adaptive uint8/16/32/64')
