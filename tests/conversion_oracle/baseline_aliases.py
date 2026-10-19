#!/usr/bin/env python3
"""Prove exact within-file baseline aliases without rewriting an NWB.

Canonical identities come from input-only authored paths. UUIDs and native HDF5
addresses are measured separately. The proof permits only a comparison path map;
it does not certify publication, physical allocation, LINDI or writable HDMF graphs.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import importlib.metadata
import json
from pathlib import Path
import re
import struct
import sys
import zlib

import h5py
import numpy as np
from hdmf.container import AbstractContainer, Data
from pynwb import NWBHDF5IO
import ndx_cell_culture  # Register the exact modeled namespace before typed reads.

from canonical import CanonicalCheck, VERSIONS, expected_graph, require
from inputs import SCHEMA, sha

ALIAS_ROOT = re.compile(r'~obj_pointed_by_[0-9]+')


def address(obj):
    return int(h5py.h5o.get_info(obj.id).addr)


def text(value):
    return value.decode('utf8') if isinstance(value, bytes) else value


def native_type(value):
    return value.encode().hex()


def space(value):
    return dict(kind=int(value.get_simple_extent_type()),
                dimensions=value.get_simple_extent_dims())


def stored_value(value, handle, label):
    """Lossless logical values; native HDF5 types are retained by the owner."""
    if isinstance(value, h5py.RegionReference):
        raise ValueError(label + ': region references are outside this baseline proof')
    if isinstance(value, h5py.Reference):
        return dict(type='object_reference', target=address(handle[value]) if value else None)
    if isinstance(value, h5py.Empty):
        return dict(type='empty', dtype=value.dtype.str)
    if isinstance(value, (bytes, np.bytes_)):
        return dict(type='bytes', hex=bytes(value).hex())
    if isinstance(value, str):
        return dict(type='str', value=value)
    if isinstance(value, (tuple, list)):
        return dict(type=type(value).__name__, values=[stored_value(v, handle, label + '[' + str(i) + ']')
                                                     for i, v in enumerate(value)])
    if isinstance(value, np.ndarray):
        result = dict(type='ndarray', dtype=value.dtype.str, shape=list(value.shape))
        if value.dtype.names:
            result['fields'] = [[name, stored_value(value[name], handle, label + '.' + name)] for name in value.dtype.names]
        elif value.dtype.kind == 'O':
            result['values'] = [stored_value(v, handle, label + '[' + str(i) + ']') for i, v in enumerate(value.flat)]
        else:
            result['bytes'] = value.tobytes().hex()
        return result
    if isinstance(value, np.generic):
        return dict(type='numpy_scalar', dtype=value.dtype.str, bytes=value.tobytes().hex())
    if value is None or type(value) in (bool, int):
        return dict(type=type(value).__name__, value=value)
    if type(value) is float:
        return dict(type='float64', bytes=struct.pack('>d', value).hex())
    raise ValueError(label + ': unsupported stored value ' + str(type(value)))


def attributes(obj, handle):
    return {name: dict(native_type=native_type(obj.attrs.get_id(name).get_type()),
                       space=space(obj.attrs.get_id(name).get_space()),
                       value=stored_value(obj.attrs[name], handle, obj.name + '@' + name))
            for name in obj.attrs}


def creation(obj, handle):
    props = obj.id.get_create_plist()
    require(not obj.is_virtual and not obj.external, obj.name + ': external or virtual dataset')
    return dict(layout=int(props.get_layout()), chunks=obj.chunks, maxshape=obj.maxshape,
                filters=[stored_value(props.get_filter(i), handle, obj.name + ':filter')
                         for i in range(props.get_nfilters())],
                fill_value=stored_value(obj.fillvalue, handle, obj.name + ':fill'),
                allocation_time=int(props.get_alloc_time()), fill_time=int(props.get_fill_time()),
                object_track_times=bool(props.get_obj_track_times()))


def indirection_records(obj, coordinate, mask, encoded, handle):
    """Resolve every encoded pointer using the HDF5 global-heap/object format.

    Format reference: https://portal.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html
    Scope is this baseline's 64-bit, little-endian, one-dimensional metadata.
    Encoded descriptors, heap locations and resolved bytes all remain evidence.
    """
    strings = h5py.check_string_dtype(obj.dtype)
    is_string = strings is not None and strings.length is None
    is_reference = h5py.check_ref_dtype(obj.dtype) is h5py.Reference
    if not (is_string or is_reference):
        return None
    require(handle.id.get_create_plist().get_sizes() == (8, 8) and handle.userblock_size == 0,
            obj.name + ': unsupported indirection file address format')
    require(obj.ndim == 1, obj.name + ': unsupported indirection dimensions')
    props = obj.id.get_create_plist()
    decoded = encoded
    for i in reversed(range(props.get_nfilters())):
        if mask & (1 << i):
            continue
        require(props.get_filter(i)[0] == 1, obj.name + ': unsupported indirection filter')
        decoded = zlib.decompress(decoded)
    width = 16 if is_string else 8
    require(len(decoded) == obj.chunks[0] * width, obj.name + ': complete decoded pointer records')
    records, resolved = [], []
    file_size = Path(handle.filename).stat().st_size
    with Path(handle.filename).open('rb') as stream:
        for i in range(obj.chunks[0]):
            descriptor = decoded[i * width:(i + 1) * width]
            logical_index = coordinate[0] + i
            inside = logical_index < obj.shape[0]
            if is_string:
                length, heap_address, index = struct.unpack('<IQI', descriptor)
                row = dict(descriptor_hex=descriptor.hex(), length=length, heap_address=heap_address, heap_object_index=index)
                if length == heap_address == index == 0:
                    data = b''
                else:
                    require(heap_address + 16 <= file_size, obj.name + ': heap collection range')
                    stream.seek(heap_address)
                    header = stream.read(16)
                    require(header[:8] == b'GCOL\x01\0\0\0', obj.name + ': heap collection signature/version')
                    collection_bytes = int.from_bytes(header[8:16], 'little')
                    end = heap_address + collection_bytes
                    require(end <= file_size, obj.name + ': heap collection size')
                    offset = heap_address + 16
                    found = False
                    while offset + 16 <= end:
                        stream.seek(offset)
                        object_header = stream.read(16)
                        object_index, reference_count, reserved, size = struct.unpack('<HHIQ', object_header)
                        require(reserved == 0, obj.name + ': heap object reserved bytes')
                        if object_index == index:
                            require(size == length and offset + 16 + size <= end, obj.name + ': exact heap payload length/range')
                            data = stream.read(size)
                            row.update(collection_header_hex=header.hex(), object_header_hex=object_header.hex(),
                                       object_header_offset=offset, payload_offset=offset + 16, heap_reference_count=reference_count,
                                       payload_hex=data.hex())
                            found = True
                            break
                        if object_index == 0:
                            break
                        offset += 16 + ((size + 7) // 8) * 8
                    require(found, obj.name + ': missing heap object')
                if inside:
                    require(obj[logical_index] == data, obj.name + ': encoded heap payload disagrees with native read')
                else:
                    require(data == b'', obj.name + ': nonempty indirection padding')
                resolved.append(dict(type='bytes', hex=data.hex()))
            else:
                target = int.from_bytes(descriptor, 'little')
                if inside:
                    native = obj[logical_index]
                    require(target == (address(handle[native]) if native else 0), obj.name + ': encoded object target disagrees with native read')
                else:
                    require(target == 0, obj.name + ': nonnull reference padding')
                row = dict(descriptor_hex=descriptor.hex(), target_address=target or None)
                resolved.append(dict(type='object_reference', target=target or None))
            records.append(row)
    return dict(kind='vlen_string_heap' if is_string else 'object_header_reference',
                decoded_descriptor_bytes_hex=decoded.hex(), records=records, resolved_values=resolved)


def inventory(handle):
    """Visit every logical path and every unique physical payload/attribute once."""
    physical, logical = {}, {}

    def walk(obj, path, ancestors):
        location = address(obj)
        require(location not in ancestors, path + ': cyclic HDF5 containment')
        logical[path] = location
        first = location not in physical
        if first:
            row = dict(kind='group' if isinstance(obj, h5py.Group) else 'dataset',
                       paths=[], attrs=attributes(obj, handle))
            physical[location] = row
            if isinstance(obj, h5py.Dataset):
                row.update(native_type=native_type(obj.id.get_type()), space=space(obj.id.get_space()),
                           creation=creation(obj, handle), payload=stored_value(obj[()], handle, path), chunks=[])
                row.update(storage_bytes=int(obj.id.get_storage_size()), contiguous_byte_offset=obj.id.get_offset(), indirections=[])
                if obj.chunks:
                    def chunk(info):
                        mask, payload = obj.id.read_direct_chunk(info.chunk_offset)
                        require(mask == info.filter_mask and len(payload) == info.size, path + ': actual chunk identity')
                        row['chunks'].append(dict(coordinate=list(info.chunk_offset), filter_mask=int(mask),
                            byte_offset=int(info.byte_offset), bytes=int(info.size), encoded_sha256=__import__('hashlib').sha256(payload).hexdigest()))
                        pointers = indirection_records(obj, info.chunk_offset, mask, payload, handle)
                        if pointers is not None:
                            row['indirections'].append(dict(coordinate=list(info.chunk_offset), **pointers))
                    obj.id.chunk_iter(chunk)
        physical[location]['paths'].append(path)
        if isinstance(obj, h5py.Group):
            children = {}
            for name in obj:
                link = obj.get(name, getlink=True)
                require(isinstance(link, (h5py.HardLink, h5py.SoftLink)), path + '/' + name + ': unsupported link')
                child = obj[name]
                children[name] = dict(kind=type(link).__name__, target=address(child),
                                      soft_path=link.path if isinstance(link, h5py.SoftLink) else None)
                walk(child, path.rstrip('/') + '/' + name, ancestors | {location})
            if first:
                physical[location]['children'] = children
    walk(handle['/'], '/', set())
    return physical, logical


class Equivalence:
    """Compare complete typed values and ordered reference targets, never UUID only."""

    def __init__(self, physical):
        self.physical = physical
        self.complete = set()
        self.active = set()
        self.reference_edges = []
        self.encoding_differences = []

    def value(self, left, right, label):
        require(type(left) is type(right), label + ': value representation type')
        if isinstance(left, dict):
            require(set(left) == set(right), label + ': value fields')
            if left.get('type') == 'object_reference':
                a, b = left['target'], right['target']
                require((a is None) == (b is None), label + ': reference nullness')
                self.reference_edges.append(dict(label=label, left=a, right=b))
                if a is not None:
                    self.objects(a, b, label + ': ordered reference target')
            else:
                for key in left:
                    self.value(left[key], right[key], label + '/' + key)
        elif isinstance(left, (list, tuple)):
            require(len(left) == len(right), label + ': shape/cardinality')
            for i, (a, b) in enumerate(zip(left, right)):
                self.value(a, b, label + '[' + str(i) + ']')
        else:
            require(left == right, label + ': exact typed value/bytes')

    def objects(self, left, right, label):
        pair = (left, right)
        if pair in self.complete:
            return
        require(pair not in self.active, label + ': unsupported cyclic alias-reference comparison')
        self.active.add(pair)
        a, b = self.physical[left], self.physical[right]
        require(a['kind'] == b['kind'], label + ': object kind')
        self.value(a['attrs'], b['attrs'], label + ': complete attributes')
        if a['kind'] == 'dataset':
            for key in ('native_type', 'space', 'creation', 'payload'):
                self.value(a[key], b[key], label + ':' + key)
            # Offsets are physical observations, not equal identities for copies.
            # Keep all chunks in inventory; prove equal coordinates/masks/encoded bytes.
            omit_offset = lambda rows: [{k: v for k, v in r.items() if k != 'byte_offset'} for r in rows]
            if a['indirections'] or b['indirections']:
                self.value([{k: r[k] for k in ('coordinate', 'filter_mask')} for r in a['chunks']],
                           [{k: r[k] for k in ('coordinate', 'filter_mask')} for r in b['chunks']], label + ': indirection chunks')
                self.value([dict(coordinate=r['coordinate'], kind=r['kind'], resolved_values=r['resolved_values']) for r in a['indirections']],
                           [dict(coordinate=r['coordinate'], kind=r['kind'], resolved_values=r['resolved_values']) for r in b['indirections']],
                           label + ': complete encoded pointer resolution')
                if omit_offset(a['chunks']) != omit_offset(b['chunks']):
                    self.encoding_differences.append(dict(label=label, left_address=left, right_address=right,
                        reason='Every decoded descriptor independently resolves to equal full string bytes or ordered proven object targets; physical identities remain distinct',
                        left_chunks=a['chunks'], right_chunks=b['chunks']))
            else:
                self.value(omit_offset(a['chunks']), omit_offset(b['chunks']), label + ': encoded chunks')
        else:
            require(set(a['children']) == set(b['children']), label + ': complete children')
            for name, child in a['children'].items():
                target = b['children'][name]
                require(child['kind'] == target['kind'] and child['soft_path'] == target['soft_path'], label + '/' + name + ': link kind/path')
                self.objects(child['target'], target['target'], label + '/' + name)
        self.active.remove(pair)
        self.complete.add(pair)


def loaded_inventory(io, nwb, handle, oracle):
    """Retain complete fields, Data.data and authored parent/reference identities."""
    keys = {id(obj): key for key, obj in oracle.objects.items()}

    def value(v, label):
        if isinstance(v, AbstractContainer):
            require(id(v) in keys, label + ': unanchored loaded container')
            return dict(type='authored_container_reference', key=keys[id(v)])
        if isinstance(v, dict):
            return dict(type='mapping', items=[[k, value(x, label + '/' + k)] for k, x in v.items()])
        if isinstance(v, (tuple, list)):
            return dict(type=type(v).__name__, values=[value(x, label + '[' + str(i) + ']') for i, x in enumerate(v)])
        if isinstance(v, datetime):
            return dict(type='datetime', value=v.isoformat())
        if hasattr(v, 'shape') and hasattr(v, '__getitem__') and not isinstance(v, (str, bytes, np.generic)):
            a = np.asarray(v[()] if v.shape == () else v[:])
            if a.dtype.kind == 'O':
                return dict(type='loaded_object_array', dtype=a.dtype.str, shape=list(a.shape),
                            values=[value(x, label + '[' + str(i) + ']') for i, x in enumerate(a.flat)])
            return stored_value(a, handle, label)
        return stored_value(v, handle, label)

    rows = {}
    for key, obj in oracle.objects.items():
        builder = io.manager.get_builder(obj)
        require(builder.path == 'root' or builder.path.startswith('root/'), key + ': builder path')
        path = '/' + builder.path.removeprefix('root').lstrip('/')
        require(handle[path].id == handle[oracle.nodes[key].path].id, key + ': loaded builder physical identity')
        rows[key] = dict(name=obj.name, kind=getattr(obj, 'neurodata_type', getattr(obj, 'data_type', None)),
                         namespace=obj.namespace, measured_uuid=obj.object_id, measured_hdf5_address=address(handle[path]),
                         builder_path=path, parent=keys[id(obj.parent)] if obj.parent is not None else None,
                         fields={name: value(v, key + '.' + name) for name, v in obj.fields.items()},
                         is_Data=isinstance(obj, Data))
        if isinstance(obj, Data):
            rows[key]['Data_payload'] = value(obj.data, key + '.data')
    return rows


def prove_file(path, facts_folder, receipt_path):
    require({k: importlib.metadata.version(k) for k in VERSIONS} == VERSIONS, 'Pinned baseline-proof reader runtime')
    before = sha(path)
    facts = json.loads((facts_folder / 'facts.json').read_text())
    require(facts['schema_version'] == SCHEMA == 2, 'Input-only schema-2 facts required')
    require(sha(facts_folder / facts['source']['file']) == facts['source']['sha256'], 'Original raw identity')
    require(sha(facts_folder / facts['projection']['file']) == facts['projection']['sha256'], 'Original projection identity')
    receipt = json.loads(receipt_path.read_text())
    nodes, _, _ = expected_graph(facts_folder, facts, receipt)
    with h5py.File(path, 'r') as handle:
        physical, logical = inventory(handle)
        anchors = {}
        for key, node in nodes.items():
            obj = handle[node.path]
            require(text(obj.attrs.get('neurodata_type')) == node.kind and text(obj.attrs.get('namespace')) == node.namespace,
                    key + ': authored physical kind/namespace')
            anchors[key] = dict(path=node.path, kind=node.kind, namespace=node.namespace, parent=node.parent,
                                measured_hdf5_address=address(obj), measured_uuid=text(obj.attrs['object_id']))
        require(len({a['measured_hdf5_address'] for a in anchors.values()}) == len(anchors), 'Authored physical identities collapsed')
        roots = [name for name in handle if name.startswith('~')]
        require(all(ALIAS_ROOT.fullmatch(name) for name in roots), 'Unsupported expanded baseline root name')
        # This is a measured old-copier format invariant, not an authored key.
        # Canonical ownership below still comes from the independent input paths.
        require(all(int(name.rsplit('_', 1)[1]) == address(handle[name]) for name in roots),
                'Expanded root address marker does not identify its actual physical object')
        mappings, proofs, distinct_pairs = {}, {}, set()
        for name in roots:
            obj = handle[name]
            candidates = [key for key, a in anchors.items() if text(obj.attrs.get('neurodata_type')) == a['kind']
                          and text(obj.attrs.get('namespace')) == a['namespace']]
            matches, failures = [], {}
            for key in candidates:
                proof = Equivalence(physical)
                try:
                    proof.objects(address(obj), anchors[key]['measured_hdf5_address'], name + ' -> ' + key)
                except ValueError as error:
                    failures[key] = str(error)
                else:
                    matches.append((key, proof))
            require(len(matches) == 1, name + ': no unique complete authored equivalent: ' + json.dumps(failures, sort_keys=True))
            key, proof = matches[0]
            mappings['/' + name] = anchors[key]['path']
            proofs['/' + name] = dict(authored_key=key, complete_object_pairs=sorted(proof.complete),
                ordered_reference_edges=proof.reference_edges, source_and_target_same_physical_object=address(obj) == anchors[key]['measured_hdf5_address'],
                encoded_indirection_differences=proof.encoding_differences, candidate_failures=failures)
            distinct_pairs |= {pair for pair in proof.complete if pair[0] != pair[1]}
        mapped_addresses = {a: b for a, b in distinct_pairs}
        require(all(len({b for a, b in distinct_pairs if a == source}) == 1 for source in mapped_addresses), 'Ambiguous expanded physical class')
        logical_mapping = {}
        for source, target in mappings.items():
            for old, location in logical.items():
                if old == source or old.startswith(source + '/'):
                    new = target + old[len(source):]
                    require(new in logical, old + ': missing mapped comparison path')
                    pair = (location, logical[new])
                    require(pair[0] == pair[1] or pair in distinct_pairs, old + ': unproven comparison pair')
                    logical_mapping[old] = new
        require(all(any(p in logical_mapping for p in physical[a]['paths']) for a in mapped_addresses), 'Reference equivalence escapes declared alias roots')
        # A complete strict loaded-model check independently protects authored
        # parents, all fields/Data and ordered canonical reference identities.
        with NWBHDF5IO(str(path), 'r', load_namespaces=True) as io:
            nwb = io.read()
            oracle = CanonicalCheck(nodes)
            oracle.bind(nwb)
            oracle.check_fields()
            loaded = loaded_inventory(io, nwb, handle, oracle)
        schema_versions = {name: sorted(handle['specifications'][name]) for name in handle['specifications']}
    require(sha(path) == before, 'Original NWB bytes changed during proof')
    return dict(passed=True, scope='complete within-file expanded baseline equivalence only', nwb_sha256=before,
        facts_sha256=sha(facts_folder / 'facts.json'), receipt_sha256=sha(receipt_path), reader_versions=VERSIONS,
        schema_versions=schema_versions, canonical_anchors=anchors, physical_objects=physical, logical_paths=logical,
        loaded_containers=loaded, expanded_roots=mappings, complete_alias_proofs=proofs,
        comparison_path_mapping=logical_mapping, distinct_physical_equivalence_pairs=sorted(distinct_pairs),
        counts=dict(physical_objects=len(physical), logical_paths=len(logical), loaded_containers=len(loaded),
                    Data_payloads=sum(row['is_Data'] for row in loaded.values()), expanded_roots=len(mappings),
                    expanded_logical_paths=len(logical_mapping), distinct_physical_copies=len(mapped_addresses)),
        original_bytes_unchanged=True, independent_authored_identity_not_uuid=True,
        scope_limits=['Mapping is comparison-only and bound to this exact file, facts and reader/schema versions.',
                      'All original physical offsets, encoded chunk identities, attributes, fields, references and findings remain evidence.',
                      'No writable HDMF/export/append, original routing support, physical allocation or complete artifact acceptance is inferred.',
                      'Region references, external/virtual datasets and cyclic alias-reference comparisons are explicitly unsupported.'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('nwb', 'facts', 'receipt', 'report'):
        parser.add_argument('--' + option, required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, '/opt/ephys-modular/maxwell/scripts')
    from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
    configure_bundled_maxwell_hdf5_plugin()
    result = prove_file(args.nwb, args.facts, args.receipt)
    with args.report.open('x') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps(dict(passed=True, nwb_sha256=result['nwb_sha256'], counts=result['counts'])))
