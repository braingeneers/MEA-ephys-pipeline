"""Input-only model/attribute expectations for the pinned Ecephys/Units exports.

Construct the declared graph with public PyNWB constructors, independent input
arrays and pinned producer metadata rules. Serialize it with the owning backend
to expose *every* physical mapping, then read it back for complete decoded fields.
No measured producer output is an input to compilation. Stored payload verification
remains nwb_data_guard's responsibility; this module covers metadata and ownership.
"""
from datetime import datetime
from pathlib import Path
import ast
import inspect
import json
import uuid

import numpy as np
from hdmf.container import AbstractContainer, Data
from hdmf.data_utils import DataIO
from hdmf_zarr import NWBZarrIO
from pynwb import NWBHDF5IO
from pynwb.device import Device
from pynwb.ecephys import ElectricalSeries, LFP
from pynwb.misc import Units

from nwb_data_guard import file_sha, require, typed


def _metadata(path):
    """Every scientific attribute object and group, including empty groups."""
    path = Path(path)
    records = {}
    for item in path.rglob('*'):
        name = str(item.relative_to(path))
        if name.startswith('specifications/'):
            continue
        if item.name in ('.zattrs', '.zgroup'):
            records[name] = json.loads(item.read_text())
    return records


def _identities(metadata, source_ids):
    identities, generated = {}, {}
    for name, attrs in metadata.items():
        if not name.endswith('.zattrs') or 'object_id' not in attrs:
            continue
        path = name.removesuffix('/.zattrs') if name != '.zattrs' else '.'
        identifier = attrs['object_id']
        require(type(identifier) is str and identifier not in identities, path + ': unique model object ID')
        identities[identifier] = path
        if identifier not in source_ids:
            require(str(uuid.UUID(identifier)) == identifier and uuid.UUID(identifier).version == 4,
                    path + ': generated model UUID4')
            generated[identifier] = dict(generated_object_at=path)
    return identities, generated


def _attributes(metadata, generated):
    """Canonicalize only identity/reference slots, never arbitrary science text."""
    def canonical(value, key=None):
        if key == 'object_id' and isinstance(value, str) and value in generated:
            return generated[value]
        if key == 'zarr_dtype' and isinstance(value, str) and value not in ('object', 'str', 'bytes', 'scalar'):
            # HDMF-Zarr's iterator branch uses dtype.str; its list/ndarray branch
            # uses type names. Canonicalize those backend numeric spellings only.
            # The independent Data guard still requires the exact stored dtype.
            dtype = np.dtype(value)
            require(dtype.kind in 'biufc', 'supported numeric backend dtype label')
            return dict(numeric_dtype=dtype.str)
        if isinstance(value, dict):
            return {k: canonical(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [canonical(v) for v in value]
        return value

    result = canonical(metadata)
    # NeuroConv adds new property columns via set iteration. Their order is not
    # scientific, but membership, uniqueness and decoded/stored order agreement are.
    if 'units/.zattrs' in result:
        names = result['units/.zattrs']['colnames']
        require(len(names) == len(set(names)), 'Units: unique stored column names')
        result['units/.zattrs']['colnames'] = sorted(names)
    return result


def snapshot(path, nwb, source_ids):
    """Complete decoded fields, Data bindings, parents, types and physical attrs."""
    metadata = _metadata(path)
    identities, generated = _identities(metadata, set(source_ids))
    nodes = {}

    def normalize(value):
        if isinstance(value, AbstractContainer):
            identifier = value.object_id
            require(identifier in identities, value.name + ': model object has a physical identity')
            location = identities[identifier]
            if location not in nodes:
                nodes[location] = {}
                fields = {key: normalize(item) for key, item in value.fields.items()}
                if isinstance(value, Data):
                    fields['data'] = normalize(value.data)
                if location == 'units':
                    fields['colnames'] = sorted(fields['colnames'])
                    fields['columns'] = sorted(fields['columns'], key=lambda v: v['reference'])
                parent = normalize(value.parent) if value.parent is not None else None
                nodes[location] = dict(name=value.name, namespace=value.namespace,
                                       neurodata_type=getattr(value, 'neurodata_type',
                                                              getattr(value, 'data_type', type(value).__name__)),
                                       parent=parent, fields=fields)
            return dict(reference=location)
        if isinstance(value, DataIO):
            return normalize(value.data)
        if isinstance(value, np.generic):
            return normalize(value.item())
        if isinstance(value, dict):
            return {str(key): normalize(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [normalize(item) for item in value]
        if isinstance(value, datetime):
            return dict(datetime=value.isoformat())
        if isinstance(value, bytes):
            return dict(bytes_hex=value.hex())
        if isinstance(value, float):
            return dict(float_hex=value.hex())
        if value is None or type(value) in (str, int, bool):
            return value
        if hasattr(value, 'shape'):
            array = getattr(value, 'dataset', value)
            if hasattr(array, 'path'):
                return dict(stored_array=array.path, dtype=array.dtype.str, shape=list(array.shape))
            require(isinstance(value, np.ndarray), 'unsupported unbound model array: ' + str(type(value)))
            return dict(dtype=value.dtype.str, shape=list(value.shape), values=normalize(value.tolist()))
        raise TypeError('Unsupported model metadata: ' + str(type(value)))

    # Builder discovery can materialize optional empty collections. Compare the
    # same public getters without dropping any populated or empty collection.
    for field in ('analysis', 'device_models', 'events', 'icephys_electrodes',
                  'imaging_planes', 'intervals', 'lab_meta_data', 'ogen_sites', 'scratch',
                  'stimulus', 'stimulus_template'):
        if hasattr(nwb, field):
            getattr(nwb, field)
    normalize(nwb)
    require(set(nodes) == set(identities.values()), 'complete decoded/physical model inventory')
    require(set(nwb.objects) == set(identities), 'complete NWB object registry inventory')
    if nwb.units is not None:
        names = list(nwb.units.colnames)
        attrs = metadata['units/.zattrs']['colnames']
        require(typed(names) == typed(attrs), 'Units: decoded/stored column order')
        from hdmf.common import VectorIndex
        columns = [column.name for column in nwb.units.columns if not isinstance(column, VectorIndex)]
        require(columns == names, 'Units: decoded column/name order binding')
    return dict(physical_metadata=_attributes(metadata, generated), model=nodes,
                generated_uuid_policy='UUID4 unique per physical object; source IDs exact',
                column_order_policy='new Units columns unordered, stored/decoded binding exact',
                backend_dtype_policy='numeric zarr_dtype spellings canonicalize via numpy.dtype.str; array dtype exact')


def _property_descriptions():
    # Read the pinned declared literal, not any producer output. Do not call the
    # actual writer to manufacture its own expected metadata.
    from neuroconv.tools.spikeinterface.spikeinterface import add_units_table_to_nwbfile
    source = inspect.getsource(add_units_table_to_nwbfile)
    tree = ast.parse(source)
    candidates = [node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == 'default_descriptions'
                          for target in node.targets)]
    require(len(candidates) == 1 and isinstance(candidates[0], ast.Call)
            and isinstance(candidates[0].func, ast.Name) and candidates[0].func.id == 'dict',
            'pinned NeuroConv default description rule')
    descriptions = {arg.arg: ast.literal_eval(arg.value) for arg in candidates[0].keywords}
    import hashlib
    return descriptions, hashlib.sha256(source.encode()).hexdigest()


def compile_expected(source_hdf5, facts, array_root, job_path, sorter_log, scratch):
    """Compile four role/raw profiles from inputs and declared transforms only."""
    from source_nwb import open_source_nwb, source_nwb_from_jobs
    import spikeinterface as si
    source_hdf5, array_root, scratch = Path(source_hdf5), Path(array_root), Path(scratch)
    require(file_sha(source_hdf5) == facts['source']['source_sha256'], 'metadata source identity')
    job_path = Path(job_path)
    job = json.loads(job_path.read_text())
    require(source_nwb_from_jobs([job], job_path.parent) == source_hdf5.resolve(), 'metadata job/source binding')
    require(job['input_folder'] is None, 'metadata construction supports the declared metadata-only source path')
    recording = si.load(job['recording_dict'], base_folder=job_path.parent)
    probes_info = recording.get_annotation('probes_info', None)
    require(probes_info is None or len(probes_info) != 1,
            'metadata construction default-device rule requires absent/non-single probe annotation')
    source_ids = list(facts['source']['header'])
    descriptions, rule_sha = _property_descriptions()

    def load(descriptor):
        require(descriptor['kind'] == 'numeric', 'metadata construction numeric input')
        path = array_root / descriptor['file']
        require(file_sha(path) == descriptor['sha256'], 'metadata construction array identity')
        array = np.load(path, mmap_mode='r', allow_pickle=False)
        require(array.dtype.str == descriptor['dtype'] and list(array.shape) == descriptor['shape'],
                'metadata construction array type/shape')
        return array

    unit_description = 'Units'
    sorter_sha = None
    if sorter_log is not None:
        sorter_log = Path(sorter_log)
        sorter_name = json.loads(sorter_log.read_text())['sorter_name']
        require(type(sorter_name) is str, 'input sorter name')
        unit_description += ' from ' + sorter_name.capitalize()
        sorter_sha = file_sha(sorter_log)
    profiles = {}
    for role in ('nwb_ecephys', 'nwb_units'):
        for write_raw in (False, True):
            label = role + ('-raw' if write_raw else '-lfp')
            target = scratch / label
            require(not target.exists(), 'fresh metadata expectation scratch: ' + str(target))
            with open_source_nwb(source_hdf5, 'r', NWBHDF5IO, for_writing=True) as reader:
                nwb = reader.read()
                for name, container in list(nwb.acquisition.items()):
                    if isinstance(container, ElectricalSeries):
                        nwb.acquisition.pop(name)
                nwb.set_modified()
                device = facts['source']['unit_device_name']
                for kind in ('lfp', 'raw'):
                    if kind == 'raw' and not write_raw:
                        continue
                    fact = facts['neural'][kind]
                    calibration = fact['calibration']
                    region = nwb.create_electrode_table_region(
                        region=list(fact['electrode_rows']), description='electrode_table_region')
                    series = ElectricalSeries(
                        name='ElectricalSeries' + device + ('-LFP' if kind == 'lfp' else ''),
                        description=('LFP voltage traces from ' if kind == 'lfp' else 'Voltage traces from ') + device,
                        data=load(fact['data']), timestamps=load(fact['times']), electrodes=region,
                        conversion=float.fromhex(calibration['conversion_hex']),
                        offset=float.fromhex(calibration['offset_hex']),
                        channel_conversion=calibration['channel_conversion'])
                    if kind == 'raw':
                        nwb.add_acquisition(series)
                    else:
                        module = nwb.create_processing_module(
                            name='ecephys', description='Intermediate data from extracellular electrophysiology recordings, e.g., LFP.')
                        module.add(LFP(electrical_series=series))
                if role == 'nwb_units':
                    # The input recording satisfies the producer's fallback rule;
                    # the maintained Units producer adds this Device even at zero.
                    require('Device' not in nwb.devices, 'input default-device addition precondition')
                    nwb.add_device(Device(name='Device', description='Default device'))
                    units = facts['units']
                    if units['units']:
                        table = Units(name='units', description=unit_description)
                        nwb.units = table
                        ids = load(units['unit_table_ids'])
                        means, sds = load(units['waveform_mean']), load(units['waveform_sd'])
                        for i, train in enumerate(units['spike_times']):
                            table.add_unit(id=int(ids[i]), spike_times=load(train),
                                           electrodes=list(units['electrode_rows']),
                                           waveform_mean=means[i], waveform_sd=sds[i])
                        table.add_column('unit_name', descriptions['unit_name'],
                                         data=np.asarray([str(uuid.uuid4()) for _ in ids]))
                        for name, descriptor in units['properties'].items():
                            values = load(descriptor) if descriptor['kind'] == 'numeric' else np.asarray(descriptor['values'])
                            table.add_column(name, descriptions.get(name, 'No description.'), data=values)
                with NWBZarrIO(str(target), 'w') as writer:
                    writer.export(src_io=reader, nwbfile=nwb, write_args=dict(link_data=False))
            with open_source_nwb(target, 'r', NWBZarrIO, for_writing=True) as reader:
                profiles[label] = snapshot(target, reader.read(), source_ids)
    return dict(schema_version=1, source_sha256=facts['source']['source_sha256'], source_ids=source_ids,
                source_job_sha256=file_sha(job_path), source_sorter_log_sha256=sorter_sha,
                default_device_rule='input_folder absent and recording probes_info absent/non-single',
                metadata_rule_source_sha256=rule_sha,
                derivation='input-only public constructors, pinned defaults/producer literals and backend mapping',
                profiles=profiles)


def verify_store(path, nwb, expected, role, write_raw):
    require(expected['schema_version'] == 1 and role in ('nwb_ecephys', 'nwb_units')
            and type(write_raw) is bool, 'metadata manifest role/version')
    label = role + ('-raw' if write_raw else '-lfp')
    actual = snapshot(path, nwb, expected['source_ids'])
    desired = expected['profiles'][label]
    for section in ('physical_metadata', 'model'):
        actual_section, desired_section = actual[section], desired[section]
        require(set(actual_section) == set(desired_section), 'complete ' + section + ' inventory: missing='
                + str(sorted(set(desired_section) - set(actual_section))) + ' extra='
                + str(sorted(set(actual_section) - set(desired_section))))
        for name in sorted(desired_section):
            require(typed(actual_section[name]) == typed(desired_section[name]), section + ' mismatch: ' + name)
    return dict(complete_physical_metadata=True, complete_decoded_model=True,
                model_nodes=len(actual['model']), physical_metadata_objects=len(actual['physical_metadata']),
                profile=label, source_ids_preserved=True, physical_shared_mappings_checked=True,
                generated_uuid_policy=actual['generated_uuid_policy'], column_order_policy=actual['column_order_policy'],
                backend_dtype_policy=actual['backend_dtype_policy'])
