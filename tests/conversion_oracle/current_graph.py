#!/usr/bin/env python3
"""Complete input-derived comparisons for the exact frozen current graph scope.

This profile preserves actual argv/receipts and strict canonical/storage defaults.
It adapts invocation/publication evidence only after exact source, cache, request,
runtime and immutable graph binding. It never rewrites scientific artifacts.
"""
from pathlib import Path
from datetime import datetime
from urllib.parse import urlsplit, unquote
import argparse
import base64
import importlib.metadata
import json
import stat
import sys

import h5py
import numpy as np

import artifacts
import canonical
import storage
from canonical import require, load_array
from inputs import sha, typed

SOURCE = '855cb3c17a85e7fd995e97118eec6dc348d3f6f6'
VERSION = '0.1.30'
IMAGE = 'braingeneers/modular-maxwell-to-nwb@sha256:46871688f9a47e4ba5dc292c2b4fdd428890f19c96778e8ee9857aa31e803e0d'
SCHEMA_REFERENCE_SHA256 = 'bd807a82f39bceb03abb1340ff951e8cfa2a21a58246e4fac0c7c6d4f4c83506'
NAMES = {'nwb':'output.nwb','lindi':'output.nwb.lindi.json','validation':'output.nwb.validation.json',
         'companion':'output.linked.raw.h5','verification':'conversion-verification.json'}
KINDS = {'nwb':'nwb','lindi':'nwb_lindi','validation':'nwb_validation','companion':'maxwell_companion'}


class CurrentGraphScope:
    def __init__(self, root, trusted_freeze_sha256):
        self.root = root.resolve()
        require(sha(root/'freeze.json') == trusted_freeze_sha256, 'Trusted current graph seal')
        self.freeze = json.loads((root/'freeze.json').read_text())
        actual_files, actual_links = set(), set()
        for path in root.rglob('*'):
            name = str(path.relative_to(root)); info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                require(path.readlink().as_posix() == self.freeze['symlinks'][name], 'Frozen staging link ' + name)
                require(path.resolve().is_relative_to(self.root) and path.exists(), 'Scoped staging target ' + name)
                actual_links.add(name)
            elif path.is_file() and path != root/'freeze.json':
                self.frozen(path); actual_files.add(name)
        require(actual_files == set(self.freeze['files']) and actual_links == set(self.freeze['symlinks']), 'Complete current graph seal inventory')
        for name in ('canonical.py','storage.py','artifacts.py','inputs.py'):
            require(sha(Path(__file__).with_name(name)) == sha(root/'source/tests/conversion_oracle'/name),
                    'Exact shared current comparator ' + name)
        versions = canonical.VERSIONS | storage.STORAGE_VERSIONS
        require({key:importlib.metadata.version(key) for key in versions} == versions, 'Pinned current comparison runtime')
        self.owner = root/'source/pipeline/components/maxwell'
        manifest = self.read(self.owner/'export-manifest.json')
        require(manifest['source_commit'] == SOURCE and manifest['owner_version'] == VERSION,
                'Exact current generated owner')
        require(self.read(self.owner/'image.json') == manifest['scientific_image'] and
                manifest['scientific_image']['pinned_reference'] == IMAGE, 'Exact current owner image')
        for path in (self.owner/'scripts').glob('*.py'):
            require(sha(path) == sha(Path('/opt/ephys-modular/maxwell/scripts')/path.name), 'Installed current scientific tool ' + path.name)
        self.execution = self.read(root/'reports/execution-and-ports.json')
        require(self.execution['execution_and_ports_passed'] and len(self.execution['complete_phase_observations']) == 8,
                'Complete current executed scope')

    def frozen(self, path):
        path = Path(path)
        require(path.is_file() and not path.is_symlink() and path.is_relative_to(self.root), 'Scoped current regular file ' + str(path))
        name = str(path.relative_to(self.root))
        require(name in self.freeze['files'], 'Declared current graph file ' + name)
        row = self.freeze['files'][name]
        require(path.stat().st_size == row['bytes'] and sha(path) == row['sha256'] and
                stat.S_IMODE(path.stat().st_mode) == row['mode'], 'Frozen current graph identity ' + name)
        return path

    def read(self, path):
        return json.loads(self.frozen(path).read_text())

    def uri(self, value):
        parsed = urlsplit(value)
        require(parsed.scheme == 'file' and not parsed.netloc and not parsed.query and not parsed.fragment, 'Current local artifact URI')
        return self.frozen(Path(unquote(parsed.path)))


def observations(scope, row, facts):
    """Exact current commands; mutated test rows exercise semantic guards directly."""
    plan, task, evidence = row['native_plan'], row['task'], row['evidence']
    require(plan['semantic']['owner']['workflow_version'] == VERSION and
            plan['semantic']['owner']['image'] == IMAGE and evidence['owner'] == plan['semantic']['owner'], 'Current scientific owner binding')
    require(evidence['source'] == task['raw'] and task['raw']['sha256'] == facts['source']['sha256'] and
            evidence['projection'] == task['unit']['projection'] and task['unit']['projection']['sha256'] == facts['projection']['sha256'],
            'Current source/projection identity binding')
    selected, = [value for value in facts['stores'] if value['selected']]
    require(task['raw_dataset_path'] == selected['raw_dataset_path'], 'Current selected raw binding')
    effective = plan['semantic']['settings']['effective']
    require(typed(effective) == typed(row['effective']) == typed(evidence['settings']['effective']) and
            typed(row['submitted']) == typed(plan['semantic']['settings']['submitted']) ==
            typed(evidence['settings']['submitted']), 'Complete current settings binding')
    enabled = effective['create_linked_maxwell_companion']
    require(type(enabled) is bool and enabled == (row['alias'] == 'alpha'), 'Current explicit companion option')
    directory = Path(row['task_workdir'])/'scientific-task'
    require(directory.is_relative_to(scope.root), 'Current owned task directory')
    source, projection = directory/'inputs/recording.raw.h5', directory/'inputs/projection.json'
    require(sha(scope.frozen(source)) == facts['source']['sha256'] and sha(scope.frozen(projection)) == facts['projection']['sha256'],
            'Exact current staged source/projection bytes')
    final, converted = directory/'output.nwb', directory/'converted.nwb'
    scripts = '/opt/ephys-modular/maxwell/scripts/'
    raw = task['raw_dataset_path']; python = '/usr/local/bin/python'
    convert = [python,scripts+'convert_maxone_to_nwb.py','--input-file',str(source),'--output-file',str(converted),
               '--maxwell-raw-dataset-path',raw]
    for key in ('session_description','session_identifier_prefix','session_start_time','subject_id','subject_species',
                'subject_description','nwb_chunk_duration_ms','nwb_chunk_channels','nwb_buffer_chunks'):
        convert += ['--'+key.replace('_','-'),str(effective[key])]
    convert += ['--nwb-metadata-file',str(projection)]
    commands = dict(
        validate_input=[python,scripts+'validate_maxwell_input.py','--input-file',str(source),'--maxwell-raw-dataset-path',raw,
            '--verify-all-raw-chunks','--output-raw-dataset-path-file',str(directory/'raw-path.txt')],
        convert=convert,
        repack=[python,scripts+'repack_nwb_channel_major.py','--input-file',str(converted),'--output-file',str(final)],
        companion=[python,scripts+'create_legacy_maxwell_companion.py','--input-file',str(source),'--nwb-file',str(final),
            '--output-file',str(directory/'output.linked.raw.h5'),'--nwb-sibling-filename','output.nwb','--maxwell-raw-dataset-path',raw],
        lindi=[python,scripts+'generate_lindi_metadata.py','--nwb-file',str(final),'--output-file',str(directory/'output.nwb.lindi.json'),
            '--source-url',evidence['reader_url'],'--num-dataset-chunks-threshold',str(effective['lindi_num_dataset_chunks_threshold']),
            '--contiguous-dataset-max-chunk-size',str(effective['lindi_contiguous_dataset_max_chunk_size'])],
        validate_metadata=[python,scripts+'validate_nwb_metadata.py','--nwb-file',str(final),
            '--output-file',str(directory/'output.nwb.validation.json'),'--metadata-projection',str(projection)],
        validate_outputs=[python,str(scope.owner/'runtime/verify_outputs.py'),str(directory/'checks-request.json'),str(directory/'checks.json')])
    phases = ['validate_input','convert','repack'] + (['companion'] if enabled else []) + ['lindi','validate_metadata','validate_outputs']
    require([value['phase'] for value in row['phase_observations']] == phases == [value['phase'] for value in evidence['phases']],
            'Complete current phase order')
    nwb = scope.uri(task['outputs']['nwb']); final_sha = sha(nwb)
    require(sha(scope.frozen(final)) == final_sha, 'Current staged/final publication binding')
    receipt_phases = []
    for index, (observed, owned) in enumerate(zip(row['phase_observations'], evidence['phases'], strict=True)):
        name = observed['phase']
        require(observed['command'] == commands[name], 'Exact current CLI/options: ' + name)
        require(type(observed['actual_child_exit_from_owning_report']) is int and observed['actual_child_exit_from_owning_report'] == owned['exit_code'] == 0,
                'Actual current scientific child exit')
        start, end = datetime.fromisoformat(observed['started_at_utc']), datetime.fromisoformat(observed['ended_at_utc'])
        require(start.tzinfo is not None and end >= start and observed['duration_s'] > 0, 'Actual current measured invocation window')
        require(observed['program_identity']['sha256'] == sha(Path(commands[name][1])), 'Actual current program bytes')
        if index >= 3:
            require(observed['files_before'][str(final)]['sha256'] == observed['files_after'][str(final)]['sha256'] == final_sha,
                    'Current final NWB before/after ' + name)
        receipt_phases.append(dict(phase=name,command=observed['command'],exit=observed['actual_child_exit_from_owning_report'],
            started_at_utc=observed['started_at_utc'],duration_s=observed['duration_s']))
    last = row['phase_observations'][-1]
    request_bytes = base64.b64decode(last['files_before'][str(directory/'checks-request.json')]['content_b64'],validate=True)
    result_bytes = base64.b64decode(last['files_after'][str(directory/'checks.json')]['content_b64'],validate=True)
    require(request_bytes == scope.frozen(directory/'checks-request.json').read_bytes() and
            result_bytes == scope.frozen(directory/'checks.json').read_bytes(), 'Actual current validator request/result bytes')
    request = dict(source=str(source),nwb=str(final),lindi=str(directory/'output.nwb.lindi.json'),raw_dataset_path=raw,
                   reader_url=evidence['reader_url'],projection=str(projection))
    if enabled: request['companion'] = str(directory/'output.linked.raw.h5')
    artifacts.same(json.loads(request_bytes),request,'Complete current validator request')
    shape = selected['expected_nwb_shape'];chunks = [min(shape[0],int(round(selected['rate_hz']*effective['nwb_chunk_duration_ms']/1000))),effective['nwb_chunk_channels']]
    checks = dict(schema_version=1,complete=True,signal_shape=shape,signal_values_checked=int(np.prod(shape)),
        lindi_signal_ranges_checked=((shape[0]+chunks[0]-1)//chunks[0])*((shape[1]+chunks[1]-1)//chunks[1]),
        all_signal_values_equal=True,all_lindi_signal_ranges_equal=True,all_linked_values_equal=True if enabled else None,modeled_projection_verified=True)
    artifacts.same(json.loads(result_bytes),checks,'Complete current validator result')
    artifacts.same(evidence['checks'],checks,'Complete current owning validator evidence')
    return dict(case=row['case'],source_sha256=facts['source']['sha256'],projection_sha256=facts['projection']['sha256'],
        selected_raw_dataset=raw,reader_url=evidence['reader_url'],settings=effective,phases=receipt_phases,
        derived_current_invocation_record=True,producer_receipts_rewritten=False,
        owning_native_source=SOURCE,owning_native_version=VERSION,owning_image=IMAGE,
        full_submitted_settings=row['submitted'],actual_validator_request_sha256=sha(directory/'checks-request.json'),
        actual_validator_result_sha256=sha(directory/'checks.json'))


def publication(scope, row):
    enabled = row['effective']['create_linked_maxwell_companion']
    keys = {'nwb','lindi','validation','verification'} | ({'companion'} if enabled else set())
    require(set(row['task']['outputs']) == keys, 'Complete current requested output inventory')
    references = {value['kind']:value['object'] for value in row['evidence']['artifacts']}
    require(set(references) == {KINDS[key] for key in keys if key != 'verification'}, 'Complete current scientific sibling inventory')
    paths = {key:scope.uri(uri) for key,uri in row['task']['outputs'].items()}
    parent = paths['nwb'].parent
    require({p.name for p in parent.iterdir()} == {NAMES[key] for key in keys}, 'Complete current publication directory')
    for key,path in paths.items():
        require(path.parent == parent and path.name == NAMES[key], 'Exact current sibling layout/name')
        ref = row['outcome']['evidence'] if key == 'verification' else references[KINDS[key]]
        require(ref['uri'] == row['task']['outputs'][key] and type(ref['size']) is int and
                ref['size'] == path.stat().st_size and ref['sha256'] == sha(path), 'Actual current publication URI/size/SHA')
    require(row['evidence']['reader_url'] == row['task']['outputs']['nwb'], 'Exact current reader publication URL')
    return paths


def current_sidecar_header(sidecar, receipt, selected):
    """Exact pinned LINDI rule, including its fewer-than-five URL occurrences.

    Expected occurrence counts come from input shape and submitted/effective chunk
    settings, never from counting the actual sidecar. The shared historical
    fixture header remains unchanged and strict.
    """
    require(set(sidecar) == {'generationMetadata','refs','templates','version'} and
            type(sidecar['version']) is int and sidecar['version'] == 1,
            'Complete current sidecar root inventory/version')
    settings = receipt['settings']; shape = selected['expected_nwb_shape']
    chunks = [min(shape[0], int(round(selected['rate_hz']*settings['nwb_chunk_duration_ms']/1000))),
              settings['nwb_chunk_channels']]
    occurrences = ((shape[0]+chunks[0]-1)//chunks[0])*((shape[1]+chunks[1]-1)//chunks[1])
    url = receipt['reader_url']; templated = occurrences >= 5
    storage.same_json(sidecar['templates'], {'u1':url} if templated else {},
                      'Exact current single-source template representation')
    for value in sidecar['refs'].values():
        if isinstance(value, list):
            require(value[0] == ('{{u1}}' if templated else url), 'Exact current range URL representation')
    phase, = [value for value in receipt['phases'] if value['phase'] == 'lindi']
    # This scope binds the actual default options, not arbitrary future policies.
    require(settings['lindi_num_dataset_chunks_threshold'] == 'none' and
            settings['lindi_contiguous_dataset_max_chunk_size'] == 20000000,
            'Reviewed current LINDI option scope')
    storage.same_json(sidecar['generationMetadata'], dict(
        sourceNwbFile=artifacts.argument(phase['command'],'--nwb-file'),sourceUrl=url,
        numDatasetChunksThreshold=None,contiguousDatasetMaxChunkSize=20000000,
        maxwellFilter401FallbackApplied=False,maxwellFilter401Codec=storage.CODEC),
        'Complete current LINDI generation metadata')
    require(artifacts.argument(phase['command'],'--source-url') == url and phase['exit'] == 0,
            'Actual current LINDI URL/exit')
    return sidecar['refs'], sidecar['templates'], url


def current_sidecar(handle, nwb_path, sidecar, receipt, decoder, truth, selected):
    refs, templates, url = current_sidecar_header(sidecar,receipt,selected)
    rows, padding = storage.native_chunks(handle,nwb_path,truth,selected,receipt['settings'],refs,url,templates,decoder)
    return storage.sidecar_contents(handle,nwb_path,sidecar,truth,rows,padding)


def compare(scope, row, output, schema_reference, *, copy_root=None):
    require(sha(schema_reference) == SCHEMA_REFERENCE_SHA256, 'Trusted independent schema reference')
    folder = scope.root/'inputs'/row['case'];facts = scope.read(folder/'facts.json')
    receipt = observations(scope,row,facts);paths = publication(scope,row)
    label = row['kind']+'-'+row['alias']+'-'+row['case']+('-bridge-copy' if copy_root else '')
    if copy_root:
        for key,path in paths.items():
            if key == 'verification':continue
            copied = scope.frozen(copy_root/path.name)
            require(sha(copied) == sha(path) and copied.stat().st_size == path.stat().st_size,'Exact bridge scientific copy bytes')
            paths[key] = copied
    receipt_path = output/(label+'-invocation.json')
    with receipt_path.open('x') as stream:stream.write(json.dumps(receipt,indent=2)+'\n')
    science = canonical.check_case(folder,paths['nwb'],receipt_path)
    selected, = [value for value in facts['stores'] if value['selected']]
    truth = load_array(folder,selected['signal'])
    from maxwell_lindi_codec import MaxwellMxwDataCodec
    with h5py.File(paths['nwb'],'r') as handle, h5py.File(schema_reference,'r') as reference:
        schema_nodes = storage.schema_cache(handle,reference)
        sidecar = current_sidecar(handle,paths['nwb'],scope.read(paths['lindi']),receipt,MaxwellMxwDataCodec(),truth,selected)
    report = scope.read(paths['validation'])
    final_path = artifacts.argument(next(p for p in receipt['phases'] if p['phase']=='repack')['command'],'--output-file')
    artifacts.report_contents(report,artifacts.expected_report(folder,facts,receipt,final_path))
    linked = artifacts.companion(scope.frozen(folder/'recording.raw.h5'),paths['companion'],paths['nwb'],facts,folder) if 'companion' in paths else None
    result = dict(label=label,science=science,schema_cache_nodes=schema_nodes,sidecar=sidecar,companion=linked,
        final_report_complete_fields_findings_and_duplicates_verified=True,actual_final_report_nwb_argument=final_path,
        complete_publication_verified=True,complete_actual_current_invocation_verified=True,
        all_native_invariants_satisfied=True,bridge_copy=bool(copy_root),scientific_expectations_from_output=False)
    with (output/(label+'.json')).open('x') as stream:stream.write(json.dumps(result,indent=2)+'\n')
    return result


def verify(scope, output, schema_reference):
    output.mkdir(parents=True,exist_ok=False)
    results = []
    for row in scope.execution['complete_phase_observations']:
        results.append(compare(scope,row,output,schema_reference))
        if row['kind'] == 'mea':
            ports = next(value for value in scope.execution['actual_output_ports'] if value['kind']=='mea' and value['alias']==row['alias'])
            unit, = [value for value in ports['actual_ports'] if value['metadata']['unit_id']==row['case']]
            results.append(compare(scope,row,output,schema_reference,copy_root=Path(unit['input'])))
        print(row['kind'],row['alias'],row['case'],'complete scientific/current artifact checks',flush=True)
    result = dict(passed=True,current_native_units=8,bridge_copies=4,cases=results,all_native_invariants_satisfied=True,
        original_native_acceptance_claimed=False,all_11_bridge_guards_complete=False,c4_parent_complete=False)
    with (output/'summary.json').open('x') as stream:stream.write(json.dumps(result,indent=2)+'\n')
    return result


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('root','output','schema-reference'):parser.add_argument('--'+name,required=True,type=Path)
    parser.add_argument('--trusted-freeze-sha256',required=True)
    args=parser.parse_args();sys.path.insert(0,'/opt/ephys-modular/maxwell/scripts')
    from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
    configure_bundled_maxwell_hdf5_plugin()
    verify(CurrentGraphScope(args.root,args.trusted_freeze_sha256),args.output,args.schema_reference)
