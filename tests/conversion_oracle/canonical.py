#!/usr/bin/env python3
"""Check complete canonical NWB science/model against independent input facts.

This fixture oracle never obtains expected scientific values from another NWB.
Physical chunk/codec, companion, sidecar and publication checks are separate.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import uuid

import h5py
import numpy as np
import ndx_cell_culture  # Register the pinned modeled schema before reading.
from hdmf.container import Data
from hdmf.spec import AttributeSpec, DatasetSpec, GroupSpec, LinkSpec
from pynwb import NWBHDF5IO, validate
import neuroconv.basedatainterface

from inputs import SCHEMA, sha, typed

VERSIONS = {'hdmf': '6.1.0', 'pynwb': '4.1.0', 'h5py': '3.16.0',
            'ndx-cell-culture': '1.0.0', 'neuroconv': '0.9.3',
            'spikeinterface': '0.104.8'}
NAMESPACES = {'core': '2.10.0', 'hdmf-common': '1.9.0',
              'hdmf-experimental': '0.6.0', 'ndx-cell-culture': '1.0.0'}


def require(condition, label):
    if not condition:
        raise ValueError(label)


def untyped(value):
    kind = value['type']
    if kind == 'object':
        return {k: untyped(v) for k, v in value['fields'].items()}
    if kind == 'array':
        return [untyped(v) for v in value['values']]
    if kind == 'integer':
        return int(value['decimal'])
    if kind == 'float64':
        return float.fromhex(value['hex'])
    if kind == 'null':
        return None
    require(kind in ('string', 'bool'), 'Unsupported authored type: ' + kind)
    return value['value']


def load_array(folder, descriptor):
    path = folder / descriptor['file']
    require(sha(path) == descriptor['sha256'], 'Input array identity: ' + str(path))
    value = np.load(path, allow_pickle=False)
    require(value.dtype.str == descriptor['dtype'] and list(value.shape) == descriptor['shape'],
            'Input array dtype/shape: ' + str(path))
    return value


def read_data(value):
    if hasattr(value, 'shape') and hasattr(value, '__getitem__'):
        return value[()] if value.shape == () else value[:]
    return value


@dataclass(frozen=True)
class Ref:
    key: str


@dataclass(frozen=True)
class RefArray:
    keys: tuple[str, ...]


@dataclass(frozen=True)
class Dates:
    start: datetime
    end: datetime


@dataclass
class Node:
    kind: str
    name: str
    namespace: str
    fields: dict
    parent: str | None
    path: str
    payload: object = None


def storage_name(kind, key):
    # Public producer naming ABI; authored keys remain the independent identity.
    import hashlib
    clean = re.sub(r'[^A-Za-z0-9_.-]+', '-', key.strip()).strip('-') or kind
    return kind + '-' + clean + '-' + hashlib.sha256(key.encode()).hexdigest()[:16]


def expected_graph(folder, facts, receipt):
    """Use original facts and pinned source rules only; no converted object input."""
    selected, = [x for x in facts['stores'] if x['selected']]
    signal = load_array(folder, selected['signal'])
    clock = load_array(folder, selected['clock'])
    nodes = {}

    def add(key, kind, name, fields, parent, path, payload=None, namespace='core'):
        require(key not in nodes, 'Duplicate expected key: ' + key)
        nodes[key] = Node(kind, name, namespace, fields, parent, path, payload)

    core = untyped(facts['expected_core'])
    session_time = datetime.fromisoformat(core['session_start_time'])
    core['session_start_time'] = session_time
    core['timestamps_reference_time'] = session_time
    conversion, = [x for x in receipt['phases'] if x['phase'] == 'convert']
    require(conversion['exit'] == 0, 'Conversion receipt is not successful')
    start = datetime.fromisoformat(conversion['started_at_utc'])
    core['file_create_date'] = Dates(start, start + timedelta(seconds=conversion['duration_s']))
    core.update(source_script='Created using NeuroConv v0.9.3',
                source_script_file_name=neuroconv.basedatainterface.__file__,
                acquisition={'ElectricalSeries': Ref('signal')},
                electrodes=Ref('electrodes'), electrode_groups={'0': Ref('group')},
                devices={'DeviceEcephys': Ref('device')}, subject=Ref('subject'),
                lab_meta_data={'experiment_context': Ref('context')})
    add('root', 'NWBFile', 'root', core, None, '/')
    interface = facts['expected_interface']
    add('device', 'Device', 'DeviceEcephys',
        {'description': interface['device']['description']}, 'root', '/general/devices/DeviceEcephys')
    add('group', 'ElectrodeGroup', '0',
        dict(description=interface['electrode_group']['description'],
             location=interface['electrode_group']['location'], device=Ref('device')),
        'root', '/general/extracellular_ephys/0')
    electrodes = selected['electrodes']
    columns = electrodes['columns']
    # NeuroConv0.9.3 adds these columns from a set. Order is semantically free;
    # the saved colnames, column objects and HDF5 attribute must still agree exactly.
    add('electrodes', 'ElectrodesTable', 'electrodes',
        dict(description='metadata about extracellular electrodes', id=Ref('electrodes/id'),
             colnames=frozenset(columns) - {'id'},
             columns=frozenset('electrodes/' + k for k in columns if k != 'id')),
        'root', '/general/extracellular_ephys/electrodes')
    descriptions = {'rel_x': 'rel_x', 'rel_y': 'rel_y', 'electrode': 'no description',
                    'channel_name': 'unique channel reference',
                    'group_name': 'Name of the ElectrodeGroup.',
                    'group': 'Reference to the ElectrodeGroup.',
                    'location': 'Location of the electrode (channel).'}
    for key, descriptor in columns.items():
        if 'file' in descriptor:
            payload = load_array(folder, descriptor)
        elif descriptor['kind'] == 'utf8':
            payload = descriptor['values']
        else:
            require(descriptor['kind'] == 'canonical_reference', 'Unknown electrode payload')
            payload = RefArray(tuple('group' for _ in descriptor['targets']))
            require(descriptor['targets'] == ['electrode_groups/0'] * electrodes['rows'],
                    'Unknown electrode group binding')
        add('electrodes/' + key, 'ElementIdentifiers' if key == 'id' else 'VectorData', key,
            {} if key == 'id' else {'description': descriptions[key]}, 'electrodes',
            nodes['electrodes'].path + '/' + key, payload, 'hdmf-common')

    model = facts['model']['nodes']
    prefixes = {'CellLine': 'line', 'CellCulture': 'culture', 'CultureProtocol': 'protocol',
                'ConstructApplication': 'application', 'GeneticVariant': 'variant'}
    for key, value in model.items():
        kind, authored_key = value['kind'], value['logical_key']
        if key == 'subject':
            name, parent, path = 'subject', 'root', '/general/subject'
        elif key == 'context':
            name, parent, path = 'experiment_context', 'root', '/general/experiment_context'
        elif key.startswith('pharmacologies/'):
            name = re.sub(r'[^A-Za-z0-9_.-]+', '-', authored_key).strip('-')
            parent, path = 'context', '/general/experiment_context/' + name
        else:
            name = storage_name(prefixes[kind], authored_key)
            parent = 'subject' if key.count('/') == 1 else key.split('/')[0] + '/' + key.split('/')[1]
            parent_name = '' if parent == 'subject' else '/' + storage_name('line' if parent.startswith('lines/') else 'culture', parent.split('/')[1])
            path = '/general/subject' + parent_name + '/' + name
        fields = untyped(value['fields'])
        if key == 'subject':
            fields['age__reference'] = 'birth'  # PyNWB4.1 Subject default.
        for field in list(fields):
            rule = facts['model']['numeric_storage_normalization'].get(key + '/' + field)
            if rule is not None:
                require(rule['declared_schema_dtype'] == '<f4' and rule['stored_dtype'] == '<f8',
                        'Unsupported scalar storage rule')
                fields[field] = float.fromhex(rule['stored_float_hex'])
        for field, targets in value['references'].items():
            if field in ('parent_cell_line', 'source_lines', 'parent_cultures'):
                fields[field] = Ref(key + '/@' + field)
            else:
                require(len(targets) == 1, 'Scalar reference cardinality')
                fields[field] = Ref(targets[0])
        for field, targets in value['contained'].items():
            if field == 'culture_protocol':
                require(len(targets) == 1, 'Protocol cardinality')
                fields[field] = Ref(targets[0])
            else:
                fields['pharmacologys' if field == 'pharmacologies' else field] = targets
        add(key, kind, name, fields, parent, path, namespace='ndx-cell-culture')
    # Containment maps use names derived from authored keys, never output names.
    for key, value in model.items():
        for field, targets in value['contained'].items():
            if field != 'culture_protocol':
                nodes[key].fields['pharmacologys' if field == 'pharmacologies' else field] = {
                    nodes[target].name: Ref(target) for target in targets}
    for value in facts['model']['Data_reference_payloads']:
        owner, field = value['owner'], value['field']
        add(owner + '/@' + field, value['kind'], field, {'description': value['description']},
            owner, nodes[owner].path + '/' + field, RefArray(tuple(value['targets'])), value['namespace'])

    series = interface['electrical_series']
    add('signal', 'ElectricalSeries', 'ElectricalSeries',
        dict(starting_time=float.fromhex(series['starting_time_hex']), rate=float(series['rate_hz']),
             resolution=float.fromhex(series['resolution_hex']), comments=series['comments'],
             description=series['description'], conversion=float.fromhex(selected['expected_nwb_conversion_hex']),
             offset=float.fromhex(series['offset_hex']), unit=series['unit'], data=signal,
             starting_time_unit='seconds', electrodes=Ref('signal/electrodes')),
        'root', '/acquisition/ElectricalSeries')
    add('signal/electrodes', 'DynamicTableRegion', 'electrodes',
        dict(description='electrode_table_region', table=Ref('electrodes')), 'signal',
        '/acquisition/ElectricalSeries/electrodes', np.asarray(electrodes['signal_region']['rows'], dtype='<i8'), 'hdmf-common')
    require(len(nodes) == interface['canonical_containers_including_Data'] == 30, 'Expected node inventory')
    require(sum(n.payload is not None for n in nodes.values()) == interface['all_Data_payloads_including_signal_region'] == 13,
            'Expected Data inventory')
    return nodes, selected, clock


class CanonicalCheck:
    def __init__(self, nodes):
        self.nodes = nodes
        self.objects = {}
        self.references = 0
        self.numeric_values = 0
        self.scalars = 0
        self.physical_links = 0

    def physical_path(self, path):
        return path

    def physical_matches(self, actual, expected):
        return actual.id == expected.id

    def compare(self, actual, expected, label, handle=None):
        if isinstance(expected, Ref):
            if handle is None:
                require(actual is self.objects[expected.key], label + ': canonical object identity')
            else:
                require(isinstance(actual, h5py.Reference) and bool(actual), label + ': physical object reference')
                require(self.physical_matches(handle[actual], handle[self.nodes[expected.key].path]),
                        label + ': physical canonical target')
            self.references += 1
            return
        actual = read_data(actual)
        if isinstance(expected, RefArray):
            values = np.asarray(actual, dtype=object)
            require(values.shape == (len(expected.keys),), label + ': reference shape/cardinality')
            for i, key in enumerate(expected.keys):
                self.compare(values[i], Ref(key), label + '[' + str(i) + ']', handle)
            return
        if isinstance(expected, np.ndarray):
            actual = np.asarray(actual)
            require(actual.dtype == expected.dtype and actual.shape == expected.shape, label + ': dtype/shape')
            require(actual.tobytes() == expected.tobytes(), label + ': exact array values')
            self.numeric_values += int(expected.size)
            return
        if isinstance(expected, dict):
            require(isinstance(actual, dict) and set(actual) == set(expected), label + ': collection keys')
            for key, value in expected.items():
                self.compare(actual[key], value, label + '/' + key, handle)
            return
        if isinstance(expected, Dates):
            require(len(actual) == 1, label + ': creation date count')
            value = actual[0]
            if isinstance(value, bytes):
                value = value.decode('utf8')
            value = datetime.fromisoformat(value) if isinstance(value, str) else value
            require(isinstance(value, datetime) and value.tzinfo is not None,
                    label + ': timezone-aware creation date')
            require(expected.start <= value <= expected.end, label + ': creation date outside conversion invocation')
            return
        if isinstance(expected, datetime):
            if isinstance(actual, bytes):
                actual = actual.decode('utf8')
            actual = datetime.fromisoformat(actual) if isinstance(actual, str) else actual
            require(type(actual) is datetime and actual.tzinfo is not None and actual == expected,
                    label + ': acquisition datetime')
            return
        if isinstance(expected, (tuple, list)):
            require(isinstance(actual, (list, tuple, np.ndarray)) and len(actual) == len(expected), label + ': array length')
            require(np.shape(actual) == (len(expected),), label + ': text array shape')
            for i, item in enumerate(expected):
                self.compare(actual[i], item, label + '[' + str(i) + ']', handle)
            return
        if isinstance(actual, np.generic):
            actual = actual.item()
        if isinstance(actual, bytes):
            actual = actual.decode('utf8')
        if type(expected) is float:
            require(type(actual) is float and actual.hex() == expected.hex(), label + ': exact scalar float/type')
        else:
            require(type(actual) is type(expected) and actual == expected, label + ': exact scalar value/type')
        self.scalars += 1

    def bind(self, nwb):
        self.objects = {'root': nwb}
        # Resolve by source-defined containment; never by UUID or biological IDs.
        pending = set(self.nodes) - {'root'}
        while pending:
            progress = False
            for key in sorted(pending):
                node = self.nodes[key]
                if node.parent not in self.objects:
                    continue
                parent = self.objects[node.parent]
                children = [x for x in parent.children if x.name == node.name]
                require(len(children) == 1, key + ': unique owned child')
                self.objects[key] = children[0]
                pending.remove(key)
                progress = True
            require(progress, 'Unresolved canonical containment')
        require(len({id(x) for x in self.objects.values()}) == len(self.nodes), 'Canonical objects collapsed')
        children = nwb.all_children()
        require(len(children) == len(self.nodes) and {id(x) for x in children} == {id(x) for x in self.objects.values()},
                'Complete loaded container inventory')
        identifiers = []
        for key, node in self.nodes.items():
            obj = self.objects[key]
            kind = getattr(obj, 'neurodata_type', getattr(obj, 'data_type', None))
            require(kind == node.kind and obj.namespace == node.namespace and obj.name == node.name,
                    key + ': kind/namespace/name')
            parsed = uuid.UUID(obj.object_id)
            require(str(parsed) == obj.object_id and parsed.version == 4, key + ': generated UUID')
            identifiers.append(obj.object_id)
            if node.parent:
                require(obj.parent is self.objects[node.parent], key + ': canonical parent identity')
        require(len(set(identifiers)) == len(identifiers), 'Duplicate canonical UUID')

    def check_fields(self):
        for key, node in self.nodes.items():
            obj = self.objects[key]
            require(set(obj.fields) == set(node.fields), key + ': complete field inventory')
            for field, expected in node.fields.items():
                actual = obj.fields[field]
                if key == 'electrodes' and field == 'colnames':
                    require(len(actual) == len(expected) and set(actual) == expected, 'Electrode colnames inventory')
                elif key == 'electrodes' and field == 'columns':
                    order = obj.fields['colnames']
                    require(len(actual) == len(order), 'Electrode column count')
                    require(all(column is self.objects['electrodes/' + name] for name, column in zip(order, actual)),
                            'Electrode ordered column bindings')
                else:
                    self.compare(actual, expected, key + '.' + field)
            require(isinstance(obj, Data) == (node.payload is not None), key + ': Data classification')
            if node.payload is not None:
                self.compare(obj.data, node.payload, key + '.data')
        require(self.objects['subject'].related_cultures is self.objects['subject'].cell_cultures,
                'related_cultures must be the complete canonical view')
        require(self.objects['context'].pharmacologies is self.objects['context'].fields['pharmacologys'],
                'pharmacologies alias')
        for key in ('pharmacologies/vehicle', 'pharmacologies/washout'):
            require(self.objects[key].experiment is self.objects['context'], key + ': owning experiment')

    def physical(self, handle, io):
        """Map expected fields through pinned schemas; reject hidden nodes/attributes."""
        entries = {}

        def entry(path, kind):
            if path not in entries:
                entries[path] = dict(kind=kind, attrs={})
            require(entries[path]['kind'] == kind, path + ': conflicting storage kind')
            if path != '/':
                entry(str(Path(path).parent), 'group')
            return entries[path]

        def location(spec, root, base, name=None):
            parts = []
            while spec is not root:
                require(spec is not None, 'Unmapped spec ancestor')
                part = spec.name
                if part is None:
                    require(name is not None, 'Unnamed storage spec without canonical target')
                    part, name = name, None
                parts.append(part)
                spec = spec.parent
            return (base.rstrip('/') + '/' + '/'.join(reversed(parts))).rstrip('/') or '/'

        def scalar_storage(spec, base, root, expected):
            if isinstance(spec, AttributeSpec):
                parent = location(spec.parent, root, base)
                storage_kind = 'dataset' if isinstance(spec.parent, DatasetSpec) else 'group'
                attrs = entry(parent, storage_kind)['attrs']
                require(spec.name not in attrs, parent + ': duplicate expected attribute')
                attrs[spec.name] = expected
            else:
                require(isinstance(spec, DatasetSpec), 'Unsupported scalar storage spec')
                entry(location(spec, root, base), 'dataset')['value'] = expected

        for key, node in self.nodes.items():
            obj = self.objects[key]
            physical = entry(node.path, 'dataset' if isinstance(obj, Data) else 'group')
            physical['attrs'].update(namespace=node.namespace, neurodata_type=node.kind, object_id=obj.object_id)
            if node.payload is not None:
                physical['value'] = node.payload
            mapper = io.manager.type_map.get_map(obj)
            for field, expected in node.fields.items():
                spec = mapper.get_attr_spec(field)
                require(spec is not None, key + '.' + field + ': unmapped physical field')
                if key == 'electrodes' and field == 'colnames':
                    expected = list(obj.fields['colnames'])
                elif key == 'electrodes' and field == 'columns':
                    expected = {self.nodes[k].name: Ref(k) for k in sorted(expected)}
                if isinstance(expected, dict) and all(isinstance(v, Ref) for v in expected.values()):
                    targets = list(expected.values())
                elif isinstance(expected, Ref) and not isinstance(spec, AttributeSpec):
                    targets = [expected]
                else:
                    scalar_storage(spec, node.path, mapper.spec, expected)
                    continue
                for target in targets:
                    target_node = self.nodes[target.key]
                    path = location(spec, mapper.spec, node.path, target_node.name)
                    if isinstance(spec, LinkSpec):
                        entry(path, 'link')['target'] = target.key
                    else:
                        require(path == target_node.path, key + '.' + field + ': physical containment')

        root = entries['/']
        root['attrs']['nwb_version'] = NAMESPACES['core']
        root['attrs']['.specloc'] = 'schema-cache-reference'
        # Required untyped core groups come from the pinned NWBFile schema. Other
        # optional ancestors have already been created by expected field mappings.
        root_spec = io.manager.type_map.get_map(self.objects['root']).spec

        def required_groups(spec, path):
            for child in spec.groups:
                if child.name is not None and child.data_type is None and child.required:
                    target = path.rstrip('/') + '/' + child.name
                    entry(target, 'group')
                    required_groups(child, target)
        required_groups(root_spec, '/')
        require('.specloc' in handle.attrs and isinstance(handle.attrs['.specloc'], h5py.Reference), 'Schema cache pointer')
        cache = handle[handle.attrs['.specloc']]
        require(cache.name == '/specifications' and set(cache) == set(NAMESPACES), 'Cached namespace inventory')
        for namespace, version in NAMESPACES.items():
            require(set(cache[namespace]) == {version}, namespace + ': cached namespace version')
        visited = set()

        def walk(path):
            if path == '/specifications':
                # Schema validation covers the cached schemas; complete cache byte/
                # sidecar storage inventory belongs to the physical-artifact checker.
                return
            expected_path = self.physical_path(path)
            require(expected_path in entries, path + ': unexpected physical object/link')
            contract = entries[expected_path]
            visited.add(expected_path)
            parent, name = str(Path(path).parent), Path(path).name
            link = handle[parent].get(name, getlink=True) if path != '/' else None
            if contract['kind'] == 'link':
                require(isinstance(link, (h5py.HardLink, h5py.SoftLink)), path + ': unsupported link')
                target = self.nodes[contract['target']].path
                require(self.physical_matches(handle[path], handle[target]), path + ': alias points to a different physical object')
                self.physical_links += 1
                return
            require(path == '/' or isinstance(link, h5py.HardLink), path + ': canonical storage must be owned')
            obj = handle[path]
            require(isinstance(obj, h5py.Dataset) == (contract['kind'] == 'dataset'), path + ': physical kind')
            require(set(obj.attrs) == set(contract['attrs']), path + ': complete physical attribute inventory')
            for field, expected in contract['attrs'].items():
                if path == '/' and field == '.specloc':
                    continue
                actual = obj.attrs[field]
                if type(expected) is float:
                    require(np.asarray(actual).dtype == np.dtype('<f8'), path + '@' + field + ': stored float dtype')
                self.compare(actual, expected, path + '@' + field, handle)
            if isinstance(obj, h5py.Group):
                for child in obj:
                    walk(path.rstrip('/') + '/' + child)
            elif 'value' in contract:
                expected = contract['value']
                if isinstance(expected, RefArray):
                    require(h5py.check_dtype(ref=obj.dtype) is h5py.Reference, path + ': reference dtype')
                elif isinstance(expected, (str, list, datetime, Dates)):
                    info = h5py.check_string_dtype(obj.dtype)
                    # HDMF's isodatetime conversion uses ASCII; ordinary text
                    # uses UTF-8. Preserve the pinned schema's distinct encodings.
                    encoding = 'ascii' if isinstance(expected, (datetime, Dates)) else 'utf-8'
                    require(info is not None and info.encoding == encoding, path + ': text dtype')
                elif type(expected) is float:
                    require(obj.dtype == np.dtype('<f8'), path + ': scalar float dtype')
                self.compare(obj, expected, path, handle)
            else:
                raise ValueError(path + ': dataset payload has no independent contract')
        walk('/')
        require(visited == set(entries), 'Expected physical paths are missing')
        addresses = [h5py.h5o.get_info(handle[node.path].id).addr for node in self.nodes.values()]
        require(len(set(addresses)) == len(addresses), 'Different canonical nodes share physical storage')
        return len(entries)


def check_case(folder, nwb_path, receipt_path):
    facts = json.loads((folder / 'facts.json').read_text())
    require(facts['schema_version'] == SCHEMA == 2, 'Current independent facts required')
    require(sha(folder / facts['source']['file']) == facts['source']['sha256'], 'Source identity')
    require(sha(folder / facts['projection']['file']) == facts['projection']['sha256'], 'Projection identity')
    projection = json.loads((folder / facts['projection']['file']).read_text())
    require(typed(projection) == facts['projection']['original_typed'], 'Original typed projection')
    receipt = json.loads(receipt_path.read_text())
    require(receipt['case'] == facts['case'] and receipt['source_sha256'] == facts['source']['sha256']
            and receipt['projection_sha256'] == facts['projection']['sha256'], 'Invocation source/projection binding')
    nodes, selected, clock = expected_graph(folder, facts, receipt)
    require(receipt['selected_raw_dataset'] == selected['raw_dataset_path'], 'Invocation selected raw path')
    errors = validate(path=str(nwb_path))
    require(not errors, 'NWB schema errors: ' + str(errors))
    oracle = CanonicalCheck(nodes)
    with NWBHDF5IO(str(nwb_path), 'r', load_namespaces=True) as io:
        nwb = io.read()
        oracle.bind(nwb)
        oracle.check_fields()
        series = oracle.objects['signal']
        actual_clock = series.starting_time + np.arange(series.data.shape[0], dtype='<f8') / series.rate
        oracle.compare(actual_clock, clock, 'Complete acquisition clock')
        require(float(series.conversion).hex() == selected['expected_nwb_conversion_hex'], 'Exact calibrated volts gain')
        require(float(series.offset).hex() == selected['offset_hex'], 'Exact signal offset')
        with h5py.File(nwb_path, 'r') as handle:
            paths = oracle.physical(handle, io)
    return dict(case=facts['case'],passed=True,facts_sha256=sha(folder / 'facts.json'),
                source_sha256=facts['source']['sha256'], projection_sha256=facts['projection']['sha256'],
                nwb_sha256=sha(nwb_path), receipt_sha256=sha(receipt_path), schema_valid=True,
                canonical_nodes=len(nodes), Data_payloads=13, physical_contract_paths=paths,
                scalar_comparisons=oracle.scalars, exact_numeric_values_checked=oracle.numeric_values,
                canonical_and_physical_reference_comparisons=oracle.references,
                physical_aliases_checked=oracle.physical_links, selected_raw_dataset=selected['raw_dataset_path'],
                selected_well=selected['well'], selected_store=selected['store'],
                complete_loaded_field_and_physical_attribute_inventory=True,
                table_column_order_normalization='Unordered membership with exact saved colnames/column/HDF5 binding.',
                expected_science_from_converted_output=False)


def verify(inputs, artifacts, receipts):
    require({k: importlib.metadata.version(k) for k in VERSIONS} == VERSIONS, 'Pinned scientific runtime versions')
    sys.path.insert(0, '/opt/ephys-modular/maxwell/scripts')
    from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
    configure_bundled_maxwell_hdf5_plugin()
    manifest = json.loads((inputs / 'input-set.json').read_text())
    require(manifest['schema_version'] == SCHEMA, 'Input manifest schema')
    require(len(manifest['cases']) == 2 and {x['case'] for x in manifest['cases']} == {'maxone', 'maxtwo_routed'},
            'Complete two-layout input inventory')
    results = []
    for row in manifest['cases']:
        case = row['case']
        require(sha(inputs / case / 'facts.json') == row['facts_sha256'], 'Input manifest fact identity')
        result = check_case(inputs / case, artifacts / case / 'output.nwb', receipts / (case + '.json'))
        require(result['source_sha256'] == row['source_sha256'] and result['projection_sha256'] == row['projection_sha256'],
                'Manifest source/projection binding')
        results.append(result)
    recordings = json.loads((inputs / 'recordings.json').read_text())
    bindings = json.loads((inputs / 'bindings.json').read_text())
    require(json.loads((inputs / 'transport-facts.json').read_text()) == dict(recordings=typed(recordings), bindings=typed(bindings)),
            'Original transport JSON types, values, signs and unknown fields')
    return dict(passed=True,scope='canonical_model_and_neural_science_only',versions=VERSIONS,cases=results,
                source_output_agreement_used_as_scientific_truth=False,all_parent_gates_complete=False,
                limits=['Chunk pipelines/masks/allocation/native ranges, companion, sidecars and publication/report checks remain separate.',
                        'Cached schema namespace versions and actual NWB validation checked; full cache storage inventory remains separate.',
                        'Strict native physical graph only; no unproven expanded-baseline alias normalization.',
                        'Positive checking alone does not prove meaningful corruption controls or actual Nextflow graph parity.'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('inputs', 'artifacts', 'receipts', 'report'):
        parser.add_argument('--' + option, required=True, type=Path)
    args = parser.parse_args()
    result = verify(args.inputs, args.artifacts, args.receipts)
    with args.report.open('x') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
