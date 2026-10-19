#!/usr/bin/env python3
"""Verify every original artifact and retain failed native invariants explicitly.

Original companion copies are all checked; they are not collapsed into aliases or
rewritten as newer native output. Scientific expectations remain input-derived.
"""
from pathlib import Path
from datetime import datetime
import argparse
import hashlib
import json
import math
import sys

import h5py
import numpy as np

import artifacts
import canonical
import storage
import original_nwb
from canonical import require, load_array
from inputs import sha

SIGNAL = '/acquisition/ElectricalSeries/data'


def actual_source(scope, case, receipt):
    relative = 'inputs/maxone/recording.raw.h5' if case == 'maxone' else 'views/maxtwo-selected-reference.raw.h5'
    source = scope.frozen(scope.original, scope.original_freeze, relative)
    require(sha(source) == receipt['source_sha256'], 'Actual original companion source identity')
    return source


def companion(source, target, nwb, folder, facts, receipt):
    selected, = [row for row in facts['stores'] if row['selected']]
    truth = load_array(folder, selected['signal'])
    raw_path = receipt['selected_raw_dataset']
    classes, reverse, logical, raw_rows = {}, {}, [], []
    counts = dict(metadata_values=0, source_attributes=0, raw_sample_values=0, copied_raw_chunks=0)
    with h5py.File(source, 'r') as original, h5py.File(target, 'r') as linked:
        raw_id = original[raw_path].id
        def walk(left, right, path, ancestors):
            require(type(left) is type(right), path + ': original companion object type')
            a, b = int(h5py.h5o.get_info(left.id).addr), int(h5py.h5o.get_info(right.id).addr)
            require(a not in ancestors, path + ': cyclic original companion ownership')
            require(b not in reverse or reverse[b] == a, path + ': distinct source objects collapsed')
            reverse[b] = a
            row = classes.setdefault(a, dict(source_paths=[], target_paths=[], target_addresses=[]))
            row['source_paths'].append(path); row['target_paths'].append(path); row['target_addresses'].append(b)
            logical.append(dict(path=path, source_address=a, target_address=b))
            canonical_raw = path == raw_path
            extra = dict(legacy_data_bridge='virtual_dataset_to_nwb', legacy_data_bridge_target_file=nwb.name,
                         legacy_data_bridge_target_dataset=SIGNAL) if canonical_raw else {}
            require(set(right.attrs) == set(left.attrs) | set(extra), path + ': complete original companion attributes')
            for key in left.attrs:
                require(left.attrs.get_id(key).get_type().equal(right.attrs.get_id(key).get_type()), path + '@' + key + ': native source attribute type')
                artifacts.same(storage.json_value(right.attrs[key], linked), storage.json_value(left.attrs[key], original),
                               path + '@' + key + ': complete original source attribute')
                counts['source_attributes'] += 1
            for key, value in extra.items():
                artifacts.same(storage.json_value(right.attrs[key], linked), value, path + '@' + key + ': actual original bridge marker')
                attribute = right.attrs.get_id(key)
                info = h5py.check_string_dtype(attribute.dtype)
                require(attribute.shape == () and info is not None and info.encoding == 'utf-8' and info.length is None,
                        path + '@' + key + ': original bridge marker native type')
            if isinstance(left, h5py.Group):
                require(set(left) == set(right), path + ': complete original companion children')
                for name in left:
                    require(isinstance(left.get(name, getlink=True), h5py.HardLink) and
                            isinstance(right.get(name, getlink=True), h5py.HardLink), path + '/' + name + ': self-contained original companion link')
                    walk(left[name], right[name], path.rstrip('/') + '/' + name, ancestors | {a})
                return
            require(isinstance(left, h5py.Dataset) and left.id.get_type().equal(right.id.get_type()) and
                    left.dtype == right.dtype and left.shape == right.shape, path + ': exact original companion dtype/shape')
            if left.id == raw_id:
                require(right.fillvalue == left.fillvalue and not right.external, path + ': original raw fill/storage dependency')
                mappings, chunks = [], []
                if canonical_raw:
                    require(right.is_virtual and right.chunks is None and storage.pipeline(right) == [], path + ': canonical original raw VDS')
                    sources = right.virtual_sources()
                    require(len(sources) == truth.shape[1], 'Complete original per-channel VDS count')
                    for channel, item in enumerate(sources):
                        require(item.file_name == nwb.name and Path(item.file_name).name == item.file_name and item.dset_name == SIGNAL,
                                'Complete original VDS mapping target')
                        require(item.src_space.get_simple_extent_dims() == (truth.shape[0], channel + 1) and
                                item.vspace.get_simple_extent_dims() == truth.T.shape, 'Complete original VDS extents')
                        source_blocks = [[[0, channel], [truth.shape[0] - 1, channel]]]
                        target_blocks = [[[channel, 0], [channel, truth.shape[0] - 1]]]
                        artifacts.same(item.src_space.get_select_hyper_blocklist().tolist(), source_blocks, 'Complete original VDS source selection')
                        artifacts.same(item.vspace.get_select_hyper_blocklist().tolist(), target_blocks, 'Complete original VDS target selection')
                        require(item.src_space.get_select_npoints() == item.vspace.get_select_npoints() == truth.shape[0], 'Complete original VDS point coverage')
                        mappings.append(dict(channel=channel, source_blocks=source_blocks, target_blocks=target_blocks))
                else:
                    require(not right.is_virtual and left.id.get_create_plist().equal(right.id.get_create_plist()),
                            path + ': retained original raw copy creation properties')
                    source_chunks, target_chunks = {}, {}
                    def collect_source(info): source_chunks[tuple(info.chunk_offset)] = info
                    def collect_target(info): target_chunks[tuple(info.chunk_offset)] = info
                    left.id.chunk_iter(collect_source)
                    right.id.chunk_iter(collect_target)
                    require(len(source_chunks) == left.id.get_num_chunks() and len(target_chunks) == right.id.get_num_chunks(),
                            path + ': complete retained raw chunk count')
                    require(set(source_chunks) == set(target_chunks), path + ': complete retained raw chunk coordinates')
                    for coordinate, info in source_chunks.items():
                        a_mask, a_bytes = left.id.read_direct_chunk(coordinate)
                        b_mask, b_bytes = right.id.read_direct_chunk(coordinate)
                        require(a_mask == b_mask == info.filter_mask == target_chunks[coordinate].filter_mask and a_bytes == b_bytes,
                                path + ': exact retained original raw chunk bytes/mask')
                        chunks.append(dict(coordinate=list(coordinate), source_offset=int(info.byte_offset),
                            target_offset=int(target_chunks[coordinate].byte_offset), bytes=len(b_bytes), mask=int(b_mask),
                            sha256=hashlib.sha256(b_bytes).hexdigest()))
                    counts['copied_raw_chunks'] += len(chunks)
                artifacts.same_payload(np.asarray(right[:]), truth.T,
                    path + (': VDS samples from input facts' if canonical_raw else ': copied raw samples from input facts'))
                counts['raw_sample_values'] += int(truth.size)
                raw_rows.append(dict(path=path, source_address=a, target_address=b, virtual=right.is_virtual,
                                     mappings=mappings, retained_native_chunks=chunks))
            else:
                require(not right.is_virtual and not right.external and left.id.get_create_plist().equal(right.id.get_create_plist()),
                        path + ': complete original metadata creation/storage')
                a_data, b_data = np.asarray(left[()]), np.asarray(right[()])
                artifacts.same_payload(b_data, a_data, path + ': complete original raw-source metadata')
                counts['metadata_values'] += int(a_data.size)
        walk(original['/'], linked['/'], '/', set())
        shared_aliases = all(len(set(row['target_addresses'])) == 1 for row in classes.values())
        require(len(raw_rows) == 3 and sum(row['virtual'] for row in raw_rows) == 1,
                'Frozen original companion has one VDS and two explicitly retained raw copies')
        root_attrs = {k:storage.json_value(v, linked) for k,v in linked.attrs.items()}
    failed = []
    if not shared_aliases: failed.append('Shared source-alias physical identity')
    if len({r['target_address'] for r in raw_rows}) != 1: failed.append('Selected raw has one VDS identity across all well/store aliases')
    return dict(completed=True, all_original_logical_samples_and_metadata_match=True,
                source_sha256=sha(source), companion_sha256=sha(target), logical_objects=logical, source_object_classes=classes,
                root_attributes=root_attrs, raw_objects=raw_rows, failed_native_invariants=failed,
                native_shared_source_alias_identity_satisfied=shared_aliases,
                native_selected_scope_markers_present='braingeneers_linked_companion_scope' in root_attrs,
                raw_copies_collapsed_or_rewritten=False, **counts)


def invocations(receipt, case, option, final_sha):
    settings = receipt['settings']
    require(set(settings) == {'nwb_chunk_duration_ms','nwb_chunk_channels','nwb_buffer_chunks','create_linked_maxwell_companion'} and
            type(settings['create_linked_maxwell_companion']) is bool and settings['create_linked_maxwell_companion'] == (option == 'enabled'),
            'Explicit original options match the requested companion mode')
    require(all(type(settings[k]) in (int,float) and math.isfinite(settings[k]) and settings[k] > 0
                for k in ('nwb_chunk_duration_ms','nwb_chunk_channels','nwb_buffer_chunks')), 'Actual original numeric settings')
    enabled = settings['create_linked_maxwell_companion']
    phases = receipt['phases']
    names = ['validate_input','convert','repack','validate_metadata'] + (['companion'] if enabled else []) + ['lindi']
    require([p['phase'] for p in phases] == names and all(type(p['exit']) is int and p['exit'] == 0 for p in phases),
            'Complete original scientific phase order and actual exits')
    folder = Path('/evidence/runs') / option / case
    final = str(folder / 'output.nwb')
    raw = receipt['selected_raw_dataset']
    source = '/inputs/maxone/recording.raw.h5' if case == 'maxone' else '/evidence/views/maxtwo-selected-reference.raw.h5'
    tools = '/opt/workflow/scripts/'
    prefix = ['/usr/local/bin/python']
    commands = {
        'validate_input':prefix+[tools+'validate_maxwell_input.py','--input-file',source,'--maxwell-raw-dataset-path',raw,
            '--verify-all-raw-chunks','--output-raw-dataset-path-file',str(folder/'raw-path.txt')],
        'convert':prefix+[tools+'convert_maxone_to_nwb.py','--input-file',source,'--output-file',str(folder/'converted.nwb'),
            '--nwb-metadata-file','/inputs/'+case+'/projection.json','--maxwell-raw-dataset-path',raw,
            '--nwb-chunk-duration-ms',str(settings['nwb_chunk_duration_ms']),'--nwb-chunk-channels',str(settings['nwb_chunk_channels']),
            '--nwb-buffer-chunks',str(settings['nwb_buffer_chunks'])],
        'repack':prefix+[tools+'repack_nwb_channel_major.py','--input-file',str(folder/'converted.nwb'),'--output-file',final,
            '--dataset-path',SIGNAL],
        'validate_metadata':prefix+[tools+'validate_nwb_metadata.py','--nwb-file',final,'--output-file',str(folder/'output.nwb.validation.json'),
            '--metadata-projection','/inputs/'+case+'/projection.json'],
        'companion':prefix+[tools+'create_legacy_maxwell_companion.py','--input-file',source,'--nwb-file',final,
            '--output-file',str(folder/'output.linked.raw.h5'),'--nwb-sibling-filename','output.nwb','--maxwell-raw-dataset-path',raw],
        'lindi':prefix+[tools+'generate_lindi_metadata.py','--nwb-file',final,'--output-file',str(folder/'output.nwb.lindi.json'),
            '--source-url',receipt['reader_url']],
    }
    measured, previous = [], None
    for i, phase in enumerate(phases):
        require(phase['command'] == commands[phase['phase']], 'Exact original scientific CLI/options: ' + phase['phase'])
        require(type(phase['duration_s']) in (int,float) and math.isfinite(phase['duration_s']) and phase['duration_s'] > 0,
                'Actual measured original phase duration')
        started = datetime.fromisoformat(phase['started_at_utc'])
        require(started.tzinfo is not None and (previous is None or started >= previous), 'Actual ordered timezone-aware original phase clocks')
        previous = started
        measured.append(dict(phase=phase['phase'],started_at_utc=phase['started_at_utc'],duration_s=phase['duration_s']))
        if i == 2: require(phase['nwb_sha256_after'] == final_sha, 'Original final repack identity')
        if i >= 3: require(phase['nwb_sha256_before'] == phase['nwb_sha256_after'] == final_sha, 'Original final NWB before/after ' + phase['phase'])
    require(receipt['original_nextflow_orchestration_executed'] is False and receipt['test_only_local_publication'] is True,
            'Actual original CLI/local-publication scope')
    return dict(final_nwb_argument=final,actual_original_phases=names,measured_fields=measured,
                original_extra_output_test_cli_executed=False,optional_companion_interface_fabricated=False)


def sidecar(nwb, value, receipt, folder, facts):
    from maxwell_lindi_codec import MaxwellMxwDataCodec
    selected, = [row for row in facts['stores'] if row['selected']]
    truth = load_array(folder, selected['signal'])
    with h5py.File(nwb, 'r') as handle:
        refs, templates, url = storage.sidecar_header(value, receipt)
        rows, padding, order = storage.native_chunk_observations(handle,nwb,truth,selected,receipt['settings'],refs,url,templates,MaxwellMxwDataCodec())
        result = storage.sidecar_contents(handle,nwb,value,truth,rows,padding)
    result.update(native_channel_major_physical_order_satisfied=order,
                  failed_native_invariants=[] if order else ['Complete channel-major physical allocation order'],
                  original_sidecar_rewritten=False,generic_client_compatibility_claimed=False)
    return result


def verify(scope, schema_reference):
    science = original_nwb.verify(scope,schema_reference)
    results = []
    for option in ('enabled','disabled'):
        for case in ('maxone','maxtwo_routed'):
            folder,facts,receipt,nwb,binding = scope.bind(option,case)
            published = nwb.parent
            enabled,names = artifacts.publication(folder,published,receipt,'file:///evidence/published/'+option+'/')
            require(enabled == (option == 'enabled'), 'Actual original publication companion mode')
            for name in names:
                scope.frozen(scope.original,scope.original_freeze,'published/'+option+'/'+case+'/'+name)
            invocation = invocations(receipt,case,option,sha(nwb))
            artifacts.report_contents(json.loads(nwb.with_name('output.nwb.validation.json').read_text()),
                artifacts.expected_report(folder,facts,receipt,invocation['final_nwb_argument']))
            metadata = sidecar(nwb,json.loads(nwb.with_name('output.nwb.lindi.json').read_text()),receipt,folder,facts)
            linked = companion(actual_source(scope,case,receipt),nwb.with_name('output.linked.raw.h5'),nwb,folder,facts,receipt) if enabled else None
            results.append(dict(case=case,option=option,completed=True,requested_siblings=names,
                nwb_sha256=sha(nwb),source_sha256=receipt['original_source_sha256'],actual_execution_source_sha256=receipt['source_sha256'],
                producer_source=receipt['original_source_commit'],producer_image=receipt['producer_image'],
                publication_artifacts=receipt['artifacts'],invocation=invocation,complete_final_report_fields_and_findings_verified=True,
                lindi=metadata,companion=linked,all_original_siblings_unchanged=True))
            print(option,case,'complete original sibling/report/publication checks; retained native limits',flush=True)
    return dict(completed=True,scope='Complete original artifact characterization; observed failed native invariants remain failed',
        science=science,cases=results,all_original_scientific_values_and_artifact_bindings_match=True,
        all_native_invariants_satisfied=all(not row['lindi']['failed_native_invariants'] and
            (row['companion'] is None or not row['companion']['failed_native_invariants']) for row in results),
        generic_lindi_client_compatibility_claimed=False,original_parent_04e3c3_complete=False,
        original_source_rewritten=False,original_nextflow_orchestration_executed=False,
        limits=['Original channel-major physical order fails in all four files.',
                'Original companions lose physical source-alias identity and retain two complete raw copies each.',
                'Original selected reference uses MaxOne topology and is not original MaxTwo support.',
                'Generic LINDI URI/numeric-padding limits and the full original parent audit remain explicit and separate.'])


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('original','proof','schema-reference','report'): parser.add_argument('--'+name,required=True,type=Path)
    for name in ('original-freeze-sha256','proof-freeze-sha256'): parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    sys.path.insert(0,'/opt/ephys-modular/maxwell/scripts')
    scope=original_nwb.FrozenOriginalScope(args.original,args.original_freeze_sha256,args.proof,args.proof_freeze_sha256)
    result=verify(scope,args.schema_reference)
    with args.report.open('x') as stream:stream.write(json.dumps(result,indent=2)+'\n')
