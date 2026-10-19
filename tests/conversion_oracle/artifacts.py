#!/usr/bin/env python3
"""Check selected-unit companions, complete publication and final NWB reports.

Run the input-derived canonical/storage oracle first. Retained companion metadata
comes from the original input graph; report findings come from that same authored
model and pinned validator rules. Invocation receipts retain actual CLI identities.
"""
from __future__ import annotations
import argparse
import base64
from collections import Counter
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re

import h5py
import numpy as np
from ndx_cell_culture import validate_recommended_terms

import canonical
import storage
from canonical import require, load_array
from inputs import sha, typed

SIGNAL = '/acquisition/ElectricalSeries/data'
RUNTIME_SOURCE = 'fe25a992bd8de825bbb4dc8c6a077e2c8c9e43a3'
RUNTIME_VALIDATOR_SHA256 = 'd67b6f00bde94fa176209a22c00d58d27e1f68bc714b82f8311e419e8db7fa3f'
BASE_SIBLINGS = ['output.nwb', 'output.nwb.lindi.json', 'output.nwb.validation.json']


def argument(command, name):
    require(command.count(name) == 1, 'Exact invocation argument: ' + name)
    index = command.index(name)
    require(index + 1 < len(command), 'Missing invocation argument: ' + name)
    return command[index + 1]


def same(actual, expected, label):
    require(typed(actual) == typed(expected), label)


def same_payload(left, right, label):
    require(left.dtype == right.dtype and left.shape == right.shape, label + ': dtype/shape')
    if left.dtype.kind == 'O':
        require(h5py.check_string_dtype(left.dtype) is not None, label + ': unsupported reference payload')
        require(all(type(a) is type(b) and isinstance(a, (str, bytes)) and a == b
                    for a, b in zip(left.flat, right.flat)), label + ': every variable string value')
    else:
        require(left.tobytes() == right.tobytes(), label + ': complete payload')


def companion(source, target, nwb, facts, folder):
    selected, = [x for x in facts['stores'] if x['selected']]
    truth = load_array(folder, selected['signal'])
    raw_path = selected['raw_dataset_path']
    pairs, reverse, paths, virtual = {}, {}, [], []
    counters = dict(metadata_values=0, source_attributes=0, aliases=0)
    with h5py.File(source, 'r') as original, h5py.File(target, 'r') as linked:
        excluded = set()
        for row in facts['stores']:
            if not row['selected']:
                excluded.update([original[row['store']].id, original['/wells/' + row['well']].id])

        def walk(left, right, path):
            require(type(left) is type(right), path + ': companion object type')
            paths.append(path)
            if left.id in pairs:
                require(right.id == pairs[left.id], path + ': exact source alias identity')
                counters['aliases'] += 1
                return
            require(right.id not in reverse, path + ': distinct input objects collapsed')
            pairs[left.id] = right.id
            reverse[right.id] = left.id
            extra = {}
            if path == '/':
                extra = dict(braingeneers_linked_companion_scope='selected_unit',
                             braingeneers_linked_companion_mapping_count=1)
            if path == raw_path:
                extra = dict(legacy_data_bridge='virtual_dataset_to_nwb',
                    legacy_data_bridge_target_file=nwb.name,
                    legacy_data_bridge_target_dataset=SIGNAL,
                    legacy_data_bridge_source_raw_dataset=raw_path)
            require(set(right.attrs) == set(left.attrs) | set(extra), path + ': complete companion attributes')
            for key in left.attrs:
                a, b = left.attrs.get_id(key), right.attrs.get_id(key)
                try:
                    require(a.get_type().equal(b.get_type()), path + '@' + key + ': attribute type')
                finally:
                    a.close(); b.close()
                same(storage.json_value(right.attrs[key], linked), storage.json_value(left.attrs[key], original),
                     path + '@' + key + ': exact source attribute')
                counters['source_attributes'] += 1
            for key, value in extra.items():
                same(storage.json_value(right.attrs[key], linked), value, path + '@' + key + ': exact bridge marker')
                attribute = right.attrs.get_id(key)
                try:
                    string = h5py.check_string_dtype(attribute.dtype)
                    require(attribute.shape == () and
                            ((type(value) is int and attribute.dtype == np.dtype('<i8')) or
                             (type(value) is str and string is not None and
                              string.encoding == 'utf-8' and string.length is None)),
                            path + '@' + key + ': complete bridge attribute storage type')
                finally:
                    attribute.close()
            if isinstance(left, h5py.Group):
                retained = {name for name in left if left[name].id not in excluded}
                require(set(right) == retained, path + ': complete retained child inventory')
                for name in sorted(retained):
                    require(isinstance(left.get(name, getlink=True), h5py.HardLink), path + ': unsupported input link')
                    require(isinstance(right.get(name, getlink=True), h5py.HardLink), path + ': companion metadata must be local')
                    walk(left[name], right[name], path.rstrip('/') + '/' + name)
                return
            require(isinstance(left, h5py.Dataset), path + ': unsupported companion object')
            require(left.id.get_type().equal(right.id.get_type())
                    and left.dtype == right.dtype and left.shape == right.shape, path + ': companion dtype/shape')
            if path == raw_path:
                require(right.is_virtual and not right.external, 'Selected raw must be the sibling-NWB VDS')
                require(right.chunks is None and storage.pipeline(right) == [] and right.fillvalue == left.fillvalue,
                        'Complete VDS storage properties')
                sources = right.virtual_sources()
                require(len(sources) == truth.shape[1], 'Complete per-channel VDS mapping count')
                for channel, row in enumerate(sources):
                    require(row.file_name == nwb.name and Path(row.file_name).name == row.file_name
                            and row.dset_name == SIGNAL, 'Exact VDS sibling filename/dataset')
                    # HDF5 persists the source selection's minimal bounding extent.
                    require(row.src_space.get_simple_extent_dims() == (truth.shape[0], channel + 1)
                            and row.vspace.get_simple_extent_dims() == truth.T.shape, 'VDS dataspace extents')
                    source_blocks = [[[0, channel], [truth.shape[0] - 1, channel]]]
                    target_blocks = [[[channel, 0], [channel, truth.shape[0] - 1]]]
                    same(row.src_space.get_select_hyper_blocklist().tolist(), source_blocks, 'Complete source selection')
                    same(row.vspace.get_select_hyper_blocklist().tolist(), target_blocks, 'Complete virtual selection')
                    require(row.src_space.get_select_npoints() == row.vspace.get_select_npoints() == truth.shape[0],
                            'Complete VDS point coverage')
                    virtual.append(dict(channel=channel, source_blocks=source_blocks, virtual_blocks=target_blocks))
                same_payload(right[:], truth.T, 'Every companion sample from input facts')
            else:
                require(not right.is_virtual and not right.external, path + ': self-contained metadata')
                require(left.id.get_create_plist().equal(right.id.get_create_plist()), path + ': metadata creation properties')
                a, b = np.asarray(left[()]), np.asarray(right[()])
                same_payload(a, b, path)
                counters['metadata_values'] += int(a.size)
        walk(original['/'], linked['/'], '/')
        store = linked[selected['store']]
        views = ['/wells/' + selected['well'] + '/rec0000', '/recordings/rec0000/' + selected['well']]
        for path in views:
            require(linked[path].id == store.id, path + ': original selected store alias')
            same_payload(linked[path + '/groups/routed/raw'][:], truth.T, path + ': every aliased sample')
        require(len(virtual) == truth.shape[1], 'Exactly one selected raw VDS object')
    return dict(unique_objects=len(pairs), logical_paths=paths, virtual_mappings=virtual,
                selected_sample_values=int(truth.size), alias_sample_values=int(truth.size) * len(views), **counters)


def expected_report(folder, facts, receipt, nwb_argument):
    require(importlib.metadata.version('nwbinspector') == '0.7.2', 'Pinned report validator')
    nodes, _, _ = canonical.expected_graph(folder, facts, receipt)
    root, group, column = nodes['root'], nodes['group'], nodes['electrodes/electrode']
    # Source-backed contracts for the four intentional findings in these fixtures.
    require(nodes['subject'].fields['species'] == 'Mus musculus' and group.fields['location'] == 'unknown',
            'Reviewed fixture ontology finding')
    issues = []
    def issue(message, importance, check, name, kind, location=None):
        row = dict(message=message, importance=importance, severity='LOW', check_function_name=check,
                   object_name=name, object_type=kind, file_path=nwb_argument)
        if location is not None:
            row['location'] = location
        issues.append(row)
    issue("Electrode location 'unknown' is not a term in the Allen Mouse Brain CCF ontology. "
          'Please use either the full name or abbreviation from the Allen Mouse Brain Atlas '
          "(e.g., 'Primary visual area' or 'VISp'). This check can be ignored if Allen CCF "
          'terms do not meet your needs.', 'BEST_PRACTICE_VIOLATION', 'check_electrodes_location_allen_ccf',
          root.name, root.kind, '/')
    for node, location in [(group, group.path), (column, None)]:
        require(node.fields['description'] == 'no description', 'Reviewed placeholder description finding')
        issue(f"Description ('{node.fields['description']}') is a placeholder.", 'BEST_PRACTICE_SUGGESTION',
              'check_description', node.name, node.kind, location)
    publications = root.fields['related_publications']
    require(publications == ['https://example.invalid/oracle'], 'Reviewed fixture publication finding')
    for value in publications:
        issue(f"Metadata /general/related_publications '{value}' does not start with 'doi: ###' and is "
              "not an external 'doi' link.", 'BEST_PRACTICE_SUGGESTION', 'check_doi_publications', root.name, root.kind, '/')
    # The public rule catalogue and issue formatter consume input-derived model
    # fields and names. No converted model supplies the expected recommendations.
    namespace = validate_recommended_terms.__globals__
    terms, issue_type = namespace['RECOMMENDED_TERMS'], namespace['RecommendedTermIssue']
    recommendations = []
    for node in nodes.values():
        for field, allowed in terms.get(node.kind, {}).items():
            value = node.fields.get(field)
            if value is not None and value != '' and str(value) not in allowed:
                recommendations.append(str(issue_type(object_type=node.kind, object_name=node.name, field=field,
                    value=str(value), allowed_values=tuple(sorted(allowed))).message))
        if node.kind == 'GeneticVariant':
            require(node.fields.get('origin') == 'unknown', 'Reviewed fixture variant origin')
    projection = json.loads((folder / facts['projection']['file']).read_text())
    return dict(report_schema_version=1, nwb_file='output.nwb', schema_valid=True, pynwb_schema_errors=[],
                nwb_inspector=dict(issue_count=len(issues), counts_by_importance=dict(Counter(x['importance'] for x in issues)), issues=issues),
                ndx_cell_culture_recommendations=recommendations,
                uploader_completeness_warnings=[x for x in projection.get('warnings', []) if isinstance(x, dict)])


def report_contents(actual, expected):
    require(set(actual) == set(expected), 'Complete validation report root inventory')
    require(set(actual['nwb_inspector']) == set(expected['nwb_inspector']), 'Complete inspector report inventory')
    # Child traversal/report emission order is presentation only; preserve exact
    # fields, values and duplicate multiplicity of every finding.
    for name in ['ndx_cell_culture_recommendations']:
        same(dict(Counter(actual[name])), dict(Counter(expected[name])), 'Complete recommendation multiset')
    encode = lambda value: json.dumps(typed(value), sort_keys=True)
    same(dict(Counter(encode(x) for x in actual['nwb_inspector']['issues'])),
         dict(Counter(encode(x) for x in expected['nwb_inspector']['issues'])), 'Complete inspector finding multiset')
    a, b = dict(actual), dict(expected)
    a.pop('ndx_cell_culture_recommendations'); b.pop('ndx_cell_culture_recommendations')
    a['nwb_inspector'] = {k:v for k,v in actual['nwb_inspector'].items() if k != 'issues'}
    b['nwb_inspector'] = {k:v for k,v in expected['nwb_inspector'].items() if k != 'issues'}
    same(a, b, 'Complete remaining validation report fields/types')


def publication(folder, artifact_folder, receipt, publication_root):
    settings = receipt['settings']
    require(set(settings) == {'nwb_chunk_duration_ms','nwb_chunk_channels','nwb_buffer_chunks','create_linked_maxwell_companion'}
            and type(settings['create_linked_maxwell_companion']) is bool, 'Explicit effective companion option')
    enabled = settings['create_linked_maxwell_companion']
    names = BASE_SIBLINGS + (['output.linked.raw.h5'] if enabled else [])
    require(set(p.name for p in artifact_folder.iterdir()) == set(names), 'Complete requested sibling inventory')
    declared = receipt['artifacts']
    require(len(declared) == len(names) and {x['name'] for x in declared} == set(names), 'Complete publication receipt inventory')
    require(type(receipt['schema_version']) is int and receipt['schema_version'] == 1
            and receipt['test_only_local_publication'] is True and receipt['publication_verified_before_receipt'] is True,
            'Actual test-local publication receipt')
    expected_reader = publication_root.rstrip('/') + '/' + receipt['case'] + '/output.nwb'
    require(receipt['reader_url'] == expected_reader, 'Reviewed publication reader URI')
    for item in declared:
        require(set(item) == {'name','size','sha256','uri'}, 'Complete published artifact identity fields')
        path = artifact_folder / item['name']
        require(path.is_file() and not path.is_symlink() and type(item['size']) is int and item['size'] == path.stat().st_size
                and re.fullmatch('[a-f0-9]{64}', item['sha256']) and sha(path) == item['sha256'], 'Actual publication size/SHA')
        require(item['uri'] == expected_reader.rsplit('/',1)[0] + '/' + item['name'], 'Exact publication artifact URI/name')
    return enabled, names


def invocations(folder, artifacts, receipt, enabled):
    phases = receipt['phases']
    expected = ['validate_input','convert','repack'] + (['companion'] if enabled else []) + ['lindi','validate_metadata','validate_outputs']
    require([x['phase'] for x in phases] == expected and all(type(x['exit']) is int and x['exit'] == 0 for x in phases),
            'Complete actual phases from explicit effective options')
    by_name = {x['phase']:x for x in phases}
    final_path = argument(by_name['repack']['command'], '--output-file')
    final_sha = sha(artifacts / 'output.nwb')
    require(by_name['repack']['nwb_sha256_after'] == final_sha, 'Final repack identity')
    for phase in phases[3:]:
        require(phase['nwb_sha256_before'] == phase['nwb_sha256_after'] == final_sha, 'Exact final NWB before/after ' + phase['phase'])
    for name, script in [('validate_input','validate_maxwell_input.py'),('convert','convert_maxone_to_nwb.py'),
                         ('repack','repack_nwb_channel_major.py'),('lindi','generate_lindi_metadata.py'),('validate_metadata','validate_nwb_metadata.py')]:
        require(by_name[name]['command'][1] == '/opt/ephys-modular/maxwell/scripts/' + script, 'Owning scientific CLI identity: ' + name)
    raw_path = receipt['selected_raw_dataset']
    for name in ['validate_input','convert'] + (['companion'] if enabled else []):
        command = by_name[name]['command']
        require(argument(command, '--input-file') == '/inputs/' + receipt['case'] + '/recording.raw.h5'
                and argument(command, '--maxwell-raw-dataset-path') == raw_path, 'Original selected-source command binding')
    for option, key in [('--nwb-chunk-duration-ms','nwb_chunk_duration_ms'),('--nwb-chunk-channels','nwb_chunk_channels'),('--nwb-buffer-chunks','nwb_buffer_chunks')]:
        require(argument(by_name['convert']['command'], option) == str(receipt['settings'][key]), 'Actual effective scientific CLI option')
    require(argument(by_name['convert']['command'], '--nwb-metadata-file') == '/inputs/' + receipt['case'] + '/projection.json',
            'Original projection command binding')
    require(argument(by_name['convert']['command'], '--output-file') == argument(by_name['repack']['command'], '--input-file'),
            'Conversion-to-repack command binding')
    for name in ['lindi','validate_metadata'] + (['companion'] if enabled else []):
        require(argument(by_name[name]['command'], '--nwb-file') == final_path, 'Final NWB command binding: ' + name)
    for name, filename in [('lindi','output.nwb.lindi.json'),('validate_metadata','output.nwb.validation.json')]:
        require(argument(by_name[name]['command'], '--output-file') == str(Path(final_path).with_name(filename)), 'Exact report/sidecar sibling command')
    require(argument(by_name['lindi']['command'], '--source-url') == receipt['reader_url'], 'Exact LINDI command URL')
    if enabled:
        command = by_name['companion']['command']
        require(command[1] == '/opt/ephys-modular/maxwell/scripts/create_legacy_maxwell_companion.py'
                and argument(command, '--nwb-sibling-filename') == 'output.nwb'
                and argument(command, '--output-file') == str(Path(final_path).with_name('output.linked.raw.h5')), 'Exact companion CLI/sibling')
    validator = by_name['validate_outputs']['command']
    if Path(validator[1]).name == 'validate_conversion_outputs.py':
        require(enabled and validator[1] == '/opt/ephys-modular/maxwell/scripts/validate_conversion_outputs.py',
                'Legacy scientific CLI requires the explicit companion')
        require(argument(validator,'--nwb-file') == final_path and argument(validator,'--linked-maxwell-file') == str(Path(final_path).with_name('output.linked.raw.h5'))
                and argument(validator,'--original-file') == '/inputs/' + receipt['case'] + '/recording.raw.h5'
                and argument(validator,'--maxwell-raw-dataset-path') == raw_path
                and argument(validator,'--lindi-file') == str(Path(final_path).with_name('output.nwb.lindi.json')), 'Legacy final output command binding')
        require('runtime_validation' not in receipt, 'Actual legacy validator evidence kind')
    else:
        require(validator[1] == '/runtime/verify_outputs.py' and len(validator) == 4, 'Explicit maintained optional-companion validator')
        require(validator[2:] == [str(Path(final_path).with_name('verify-request.json')),
                                 str(Path(final_path).with_name('verify-result.json'))], 'Actual validator request/result filenames')
        evidence = receipt['runtime_validation']
        require(set(evidence) == {'source_commit','tool_sha256','request_bytes_b64','request_sha256','result_bytes_b64','result_sha256'}
                and evidence['source_commit'] == RUNTIME_SOURCE and evidence['tool_sha256'] == RUNTIME_VALIDATOR_SHA256,
                'Exact staged runtime validator source')
        decoded = {}
        for name in ['request','result']:
            raw = base64.b64decode(evidence[name+'_bytes_b64'], validate=True)
            require(hashlib.sha256(raw).hexdigest() == evidence[name+'_sha256'], 'Actual runtime validator bytes: ' + name)
            decoded[name] = json.loads(raw)
        request = dict(source='/inputs/'+receipt['case']+'/recording.raw.h5', nwb=final_path,
                       lindi=str(Path(final_path).with_name('output.nwb.lindi.json')), raw_dataset_path=raw_path,
                       reader_url=receipt['reader_url'], projection='/inputs/'+receipt['case']+'/projection.json')
        if enabled:
            request['companion'] = str(Path(final_path).with_name('output.linked.raw.h5'))
        same(decoded['request'], request, 'Complete actual runtime validator request')
        facts = json.loads((folder/'facts.json').read_text());selected, = [x for x in facts['stores'] if x['selected']]
        shape = selected['expected_nwb_shape'];chunks = [min(shape[0],int(round(selected['rate_hz']*receipt['settings']['nwb_chunk_duration_ms']/1000))),receipt['settings']['nwb_chunk_channels']]
        expected_checks = dict(schema_version=1, complete=True, signal_shape=shape, signal_values_checked=int(np.prod(shape)),
            lindi_signal_ranges_checked=((shape[0]+chunks[0]-1)//chunks[0])*((shape[1]+chunks[1]-1)//chunks[1]),
            all_signal_values_equal=True, all_lindi_signal_ranges_equal=True, all_linked_values_equal=True if enabled else None,
            modeled_projection_verified=True)
        same(decoded['result'], expected_checks, 'Complete actual optional-companion validation result')
    return final_path


def verify(inputs, artifacts, receipts, schema_reference, publication_root='file:///evidence/published/'):
    checked = storage.verify(inputs, artifacts, receipts, schema_reference)
    cases = []
    for scientific in checked['cases']:
        case = scientific['case'];folder = inputs/case;published = artifacts/case
        facts = json.loads((folder/'facts.json').read_text());receipt = json.loads((receipts/(case+'.json')).read_text())
        enabled, names = publication(folder,published,receipt,publication_root)
        final_path = invocations(folder,published,receipt,enabled)
        report_contents(json.loads((published/'output.nwb.validation.json').read_text()), expected_report(folder,facts,receipt,final_path))
        bridge = companion(folder/'recording.raw.h5', published/'output.linked.raw.h5', published/'output.nwb',facts,folder) if enabled else None
        cases.append(dict(case=case,passed=True,effective_companion=enabled,requested_siblings=names,
                          validated_final_nwb_sha256=sha(published/'output.nwb'),companion=bridge,storage=scientific))
    return dict(passed=True,scope='native_complete_scientific_artifacts_and_test_local_publication',cases=cases,
                generic_lindi_client_compatibility_claimed=False,all_parent_gates_complete=False,
                limits=['Meaningful full corruption controls and actual original/native/bridge graphs remain separate.',
                        'Test-local publication and exact receipts do not establish live S3/Nextflow acceptance.',
                        'Generic LINDI0.4.6 URI and numeric-padding failures remain explicit.'])


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['inputs','artifacts','receipts','schema-reference','report']:
        parser.add_argument('--'+name,required=True,type=Path)
    parser.add_argument('--publication-root',default='file:///evidence/published/')
    args=parser.parse_args();result=verify(args.inputs,args.artifacts,args.receipts,args.schema_reference,args.publication_root)
    with args.report.open('x') as stream:stream.write(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='cases'},indent=2))
