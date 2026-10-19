#!/usr/bin/env python3
"""Verify native chunks, complete cached schemas and every LINDI reference.

Canonical science must pass first. HDF5 then provides physical byte/layout facts
for representation checking; it never supplies the expected neural/model values.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import h5py
import numpy as np
import numcodecs
import zarr

import canonical
from canonical import require, load_array
from inputs import sha, typed

SIGNAL = 'acquisition/ElectricalSeries/data'
CODEC = {'id': 'maxwell-mxw-data', 'hdf5_filter_id': 401, 'codec_version': 1}
STORAGE_VERSIONS = {'lindi': '0.4.6', 'zarr': '2.18.7', 'numcodecs': '0.15.1'}


def pipeline(dataset):
    props = dataset.id.get_create_plist()
    return [dict(id=row[0], flags=row[1], client_data=list(row[2]), name=row[3].decode('utf8'))
            for row in [props.get_filter(i) for i in range(props.get_nfilters())]]


def json_value(value, handle):
    """Independent primitive/reference representation; exact numeric JSON types."""
    if isinstance(value, h5py.Reference):
        require(bool(value) and not isinstance(value, h5py.RegionReference), 'Unsupported/null sidecar reference')
        target = handle[value]
        return {'_REFERENCE': {'object_id': target.attrs.get('object_id'), 'path': target.name,
                               'source': '.', 'source_object_id': handle.attrs['object_id']}}
    if isinstance(value, np.ndarray):
        return json_value(value.tolist(), handle)
    if isinstance(value, np.generic):
        return json_value(value.item(), handle)
    if isinstance(value, bytes):
        return value.decode('utf8')
    if isinstance(value, (list, tuple)):
        return [json_value(x, handle) for x in value]
    if isinstance(value, dict):
        return {k: json_value(x, handle) for k, x in value.items()}
    require(value is None or type(value) in (str, bool, int, float), 'Unsupported sidecar metadata type')
    return value


def same_json(actual, expected, label):
    require(typed(actual) == typed(expected), label + ': exact JSON types/values')


def schema_cache(handle, reference):
    require(reference['identifier'][()].decode() == 'schema-cache-reference', 'Independent schema reference identity')
    count = 0

    def compare(actual, expected):
        nonlocal count
        count += 1
        require(type(actual) is type(expected), actual.name + ': cache object type')
        require(set(actual.attrs) == set(expected.attrs), actual.name + ': cache attributes')
        for key in actual.attrs:
            a, b = actual.attrs.get_id(key), expected.attrs.get_id(key)
            try:
                require(a.get_type().equal(b.get_type()), actual.name + '@' + key + ': cache attribute dtype')
            finally:
                a.close(); b.close()
            same_json(json_value(actual.attrs[key], handle), json_value(expected.attrs[key], reference),
                      actual.name + '@' + key)
        if isinstance(actual, h5py.Group):
            require(set(actual) == set(expected), actual.name + ': cache child inventory')
            for key in actual:
                require(isinstance(actual.get(key, getlink=True), h5py.HardLink), actual.name + ': cache link')
                compare(actual[key], expected[key])
        else:
            require(not actual.is_virtual and not actual.external, actual.name + ': self-contained cache storage')
            require(actual.dtype == expected.dtype and actual.shape == expected.shape,
                    actual.name + ': cache dtype/shape')
            require(actual.chunks == expected.chunks and pipeline(actual) == pipeline(expected),
                    actual.name + ': cache chunks/filter pipeline')
            same_json(json_value(actual[()], handle), json_value(expected[()], reference), actual.name + ': cache payload')
    compare(handle['specifications'], reference['specifications'])
    return count


def native_chunk_observations(handle, nwb_path, expected, selected, settings, refs, reader_url, templates, decoder):
    dataset = handle[SIGNAL]
    duration = settings['nwb_chunk_duration_ms']
    channels = settings['nwb_chunk_channels']
    chunks = (min(expected.shape[0], int(round(selected['rate_hz'] * duration / 1000))), channels)
    require(dataset.chunks == chunks, 'Input settings determine native chunk dimensions')
    require(dataset.dtype == expected.dtype and dataset.shape == expected.shape, 'Native signal dtype/shape')
    require(pipeline(dataset) == selected['input_filter_pipeline'] ==
            [dict(id=401,flags=1,client_data=[],name='mxw-data')], 'Complete native filter pipeline')
    require(not dataset.is_virtual and not dataset.external and dataset.fillvalue == 0, 'Native signal storage/fill')
    expected_zarray = dict(zarr_format=2,shape=list(expected.shape),chunks=list(chunks),dtype=expected.dtype.str,
                          compressor=None,fill_value=0,filters=[CODEC],order='C')
    same_json(refs[SIGNAL + '/.zarray'], expected_zarray, 'Native Zarr descriptor')
    coordinates = [(t, c) for c in range(0, expected.shape[1], channels)
                   for t in range(0, expected.shape[0], chunks[0])]
    require(dataset.id.get_num_chunks() == len(coordinates), 'Complete allocated native chunk count')
    allocated_info = {}
    def collect(info):
        coordinate = tuple(info.chunk_offset)
        require(coordinate not in allocated_info, 'Duplicate allocated native chunk coordinate')
        allocated_info[coordinate] = info
    dataset.id.chunk_iter(collect)
    require(set(allocated_info) == set(coordinates), 'Complete allocated native chunk coordinate inventory')
    encoded_rows = []
    padding = 0
    file_size = nwb_path.stat().st_size
    with nwb_path.open('rb') as stream:
        for t, c in coordinates:
            info = allocated_info[(t, c)]
            mask, encoded = dataset.id.read_direct_chunk((t, c))
            require(mask == info.filter_mask == 0, 'Every native chunk must use filter401')
            require(info.size == len(encoded) and 0 <= info.byte_offset < file_size
                    and info.byte_offset + len(encoded) <= file_size, 'Allocated native range bounds')
            stream.seek(info.byte_offset)
            require(stream.read(len(encoded)) == encoded, 'Direct native chunk/range byte identity')
            key = SIGNAL + '/' + str(t // chunks[0]) + '.' + str(c // channels)
            ref = refs[key]
            require(isinstance(ref,list) and len(ref) == 3, key + ': native byte reference required')
            require(isinstance(ref[0],str) and type(ref[1]) is int and type(ref[2]) is int,
                    key + ': native byte reference field types')
            url = ref[0]
            if url.startswith('{{'):
                require(url.endswith('}}') and url[2:-2] in templates, key + ': known URL template')
                url = templates[url[2:-2]]
            require(url == reader_url and ref[1:] == [info.byte_offset, len(encoded)], key + ': exact native source/range')
            decoded = np.frombuffer(decoder.decode(encoded), dtype='<u2')
            require(decoded.size == int(np.prod(chunks)), key + ': complete decoded chunk shape')
            truth = np.zeros(chunks, dtype=expected.dtype)
            part = expected[t:t + chunks[0], c:c + channels]
            truth[:part.shape[0], :part.shape[1]] = part
            require(decoded.tobytes() == truth.tobytes(), key + ': exact input-derived samples and padding')
            padding += int(truth.size - part.size)
            encoded_rows.append(dict(coordinate=[t,c],offset=info.byte_offset,bytes=len(encoded),mask=mask,
                                     sha256=hashlib.sha256(encoded).hexdigest()))
    channel_major = [x['coordinate'] for x in sorted(encoded_rows,key=lambda x:x['offset'])] == [list(x) for x in coordinates]
    allocated = sorted(encoded_rows,key=lambda x:x['offset'])
    require(all(a['offset']+a['bytes'] <= b['offset'] for a,b in zip(allocated,allocated[1:])), 'Native ranges overlap')
    return encoded_rows, padding, channel_major


def native_chunks(handle, nwb_path, expected, selected, settings, refs, reader_url, templates, decoder):
    rows, padding, channel_major = native_chunk_observations(
        handle, nwb_path, expected, selected, settings, refs, reader_url, templates, decoder)
    require(channel_major, 'Complete channel-major physical allocation order')
    return rows, padding


def inline_contract(dataset, handle):
    """Pinned storage ABI via standard Zarr, without producer conversion helpers."""
    value = json_value(dataset[()], handle)
    store = zarr.storage.MemoryStore()
    group = zarr.group(store=store)
    if dataset.shape == ():
        array = group.create_dataset('X',shape=(1,),chunks=(1,),data=[value])
    elif dataset.dtype.kind == 'O':
        data = np.empty(dataset.shape,dtype=object)
        for index in np.ndindex(dataset.shape):
            data[index] = json_value(dataset[index],handle)
        array = group.create_dataset('X',shape=dataset.shape,chunks=dataset.shape,dtype=object,
                                     data=data,object_codec=numcodecs.JSON())
    else:
        require(dataset.dtype.kind in 'biufS', dataset.name + ': unsupported inline dtype')
        array = group.create_dataset('X',shape=dataset.shape,chunks=dataset.shape,dtype=dataset.dtype,data=dataset[()])
    return json.loads(store['X/.zarray']), array[:]


def complete_sidecar(handle, nwb_path, sidecar, receipt, decoder, expected, selected):
    require(set(sidecar) == {'generationMetadata','refs','templates','version'}
            and type(sidecar['version']) is int and sidecar['version'] == 1,
            'Complete sidecar root inventory/version')
    refs, templates = sidecar['refs'], sidecar['templates']
    reader_url = receipt['reader_url']
    require(templates == {'u1':reader_url}, 'Complete single-source reader URL templates')
    phase, = [x for x in receipt['phases'] if x['phase'] == 'lindi']
    command = phase['command']
    def argument(name):
        require(command.count(name) == 1, 'Exact LINDI command argument: ' + name)
        return command[command.index(name)+1]
    same_json(sidecar['generationMetadata'],dict(sourceNwbFile=argument('--nwb-file'),sourceUrl=reader_url,
              numDatasetChunksThreshold=None,contiguousDatasetMaxChunkSize=20000000,
              maxwellFilter401FallbackApplied=False,maxwellFilter401Codec=CODEC), 'Complete LINDI generation metadata')
    require(argument('--source-url') == reader_url and phase['exit'] == 0, 'Actual LINDI invocation URL/exit')
    rows, padding = native_chunks(handle,nwb_path,expected,selected,receipt['settings'],refs,reader_url,templates,decoder)
    allowed = set()
    store = {}
    for key, value in refs.items():
        if isinstance(value,dict):
            store[key] = json.dumps(value).encode('utf8')
        elif isinstance(value,str):
            store[key] = base64.b64decode(value[7:],validate=True) if value.startswith('base64:') else value.encode('ascii')
        else:
            require(isinstance(value,list), key + ': unsupported reference value')
    zgroup = zarr.open_group(store=store,mode='r')
    group_count = array_count = inline_values = 0

    def meta(key, expected_value):
        allowed.add(key)
        require(key in refs, key + ': missing metadata')
        same_json(refs[key], expected_value, key)

    def walk(path):
        nonlocal group_count,array_count,inline_values
        obj = handle['/' + path]
        link = handle.get('/' + path,getlink=True) if path else None
        prefix = path + '/' if path else ''
        if isinstance(link,h5py.SoftLink):
            meta(prefix+'.zgroup',{'zarr_format':2})
            meta(prefix+'.zattrs',{'_SOFT_LINK':{'path':link.path}})
            group_count += 1
            return
        require(path == '' or isinstance(link,h5py.HardLink), path + ': unsupported sidecar link')
        attrs = {key:json_value(value,handle) for key,value in obj.attrs.items()}
        if isinstance(obj,h5py.Group):
            meta(prefix+'.zgroup',{'zarr_format':2})
            group_count += 1
            if attrs:
                meta(prefix+'.zattrs',attrs)
            for name in obj:
                walk(prefix+name)
        else:
            array_count += 1
            require(not obj.is_virtual and not obj.external, path + ': sidecar requires self-contained native storage')
            if path == SIGNAL:
                meta(prefix+'.zarray',dict(zarr_format=2,shape=list(expected.shape),chunks=list(obj.chunks),
                                         dtype=expected.dtype.str,compressor=None,fill_value=0,filters=[CODEC],order='C'))
                allowed.update(SIGNAL+'/'+str(x['coordinate'][0]//obj.chunks[0])+'.'+str(x['coordinate'][1]//obj.chunks[1]) for x in rows)
            else:
                descriptor, truth = inline_contract(obj,handle)
                meta(prefix+'.zarray',descriptor)
                if obj.shape == ():
                    attrs['_SCALAR'] = True
                key = prefix + ('.'.join('0' for _ in obj.shape) if obj.shape else '0')
                allowed.add(key)
                require(isinstance(refs.get(key),str), key + ': complete inline payload required')
                actual = zgroup[path][:]
                require(actual.shape == truth.shape and actual.dtype == truth.dtype,path+': inline decoded dtype/shape')
                if actual.dtype.kind in 'biufS':
                    require(actual.tobytes() == truth.tobytes(),path+': exact inline numeric/byte payload')
                else:
                    same_json(json_value(actual,handle),json_value(truth,handle),path+': exact inline/reference payload')
                inline_values += int(actual.size)
            if attrs:
                meta(prefix+'.zattrs',attrs)
    walk('')
    require(set(refs) == allowed,'Complete sidecar reference/key inventory')
    require({key for key,value in refs.items() if isinstance(value,list)} ==
            {SIGNAL+'/'+str(x['coordinate'][0]//handle[SIGNAL].chunks[0])+'.'+str(x['coordinate'][1]//handle[SIGNAL].chunks[1]) for x in rows},
            'Only actual native chunks may be external byte ranges')
    return dict(native_chunks=rows,padding_values_checked=padding,groups=group_count,arrays=array_count,
                complete_refs=len(allowed),inline_values_checked=inline_values)


def verify(inputs,artifacts,receipts,schema_reference):
    require({k:importlib.metadata.version(k) for k in STORAGE_VERSIONS} == STORAGE_VERSIONS,'Pinned storage versions')
    science = canonical.verify(inputs,artifacts,receipts)
    sys.path.insert(0,'/opt/ephys-modular/maxwell/scripts')
    from maxwell_lindi_codec import MaxwellMxwDataCodec
    decoder = MaxwellMxwDataCodec()
    cases = []
    with h5py.File(schema_reference,'r') as reference:
        for result in science['cases']:
            case = result['case'];folder = inputs/case;nwb = artifacts/case/'output.nwb'
            facts = json.loads((folder/'facts.json').read_text())
            selected, = [x for x in facts['stores'] if x['selected']]
            expected = load_array(folder,selected['signal'])
            receipt = json.loads((receipts/(case+'.json')).read_text())
            sidecar_path = artifacts/case/'output.nwb.lindi.json'
            sidecar = json.loads(sidecar_path.read_text())
            with h5py.File(nwb,'r') as handle:
                schema_nodes = schema_cache(handle,reference)
                storage = complete_sidecar(handle,nwb,sidecar,receipt,decoder,expected,selected)
            cases.append(dict(case=case,passed=True,canonical=result,schema_cache_nodes=schema_nodes,
                              private_repack_candidate_only=receipt.get('private_repack_candidate_only',False),
                              sidecar_sha256=sha(sidecar_path),**storage))
    return dict(passed=True,scope='canonical_plus_chunks_schema_and_lindi_only',storage_versions=STORAGE_VERSIONS,
                schema_reference_sha256=sha(schema_reference),cases=cases,all_parent_gates_complete=False,
                private_repack_candidate_only=all(x['private_repack_candidate_only'] for x in cases),
                generic_lindi_client_compatibility_claimed=False,
                limits=['Generic LINDI0.4.6 numeric padding limitation remains; exact native range decoding is checked.',
                        'Strict native graph only; no unproven baseline alias normalization.',
                        'Companion, publication/report identities, meaningful corruption controls and actual Nextflow graph acceptance remain separate.'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('inputs','artifacts','receipts','schema-reference','report'):
        parser.add_argument('--'+name,required=True,type=Path)
    args = parser.parse_args()
    result = verify(args.inputs,args.artifacts,args.receipts,args.schema_reference)
    with args.report.open('x') as stream:
        stream.write(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='cases'},indent=2))
