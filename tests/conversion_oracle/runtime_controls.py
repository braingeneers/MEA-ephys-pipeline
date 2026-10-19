#!/usr/bin/env python3
"""Actual current-owner CLI rejection controls against accepted frozen science.

Preparation creates explicitly synthetic private counterexamples and real native
raw/prepare plans. Workers execute unchanged CLIs through the stdlib observer.
Checking requires actual Docker exits and intended exceptions/phases, unchanged
inputs/publication, and all unmodified current verifier positives.
"""
from pathlib import Path
import argparse
import copy
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time

GRAPH_SEAL='28e085bc5b3c2584c27bc9c4f2504e0f86ce8c2305b39fc5c86ee5d3eb6ed8be'
SIGNAL='/acquisition/ElectricalSeries/data'


def write(path,value):
    with path.open('x') as stream:stream.write(json.dumps(value,indent=2,allow_nan=False)+'\n')


def prepare(root,graph_root):
    import h5py
    import numpy as np
    import current_graph as graph
    from inputs import sha
    from canonical import require
    sys.path.insert(0,'/opt/ephys-modular/maxwell/scripts')
    from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
    configure_bundled_maxwell_hdf5_plugin()
    scope=graph.CurrentGraphScope(graph_root,GRAPH_SEAL)
    source=root/'source/pipeline/components/maxwell'
    runtime=source/'runtime';native=runtime/'native_task.py';verifier=runtime/'verify_outputs.py'
    generator=Path('/opt/ephys-modular/maxwell/scripts/generate_lindi_metadata.py')
    require((root/'cases').is_dir() and not (root/'prepared.json').exists(),'New empty control preparation')
    scenarios=[];setup=[]
    native_rows={r['case']:r for r in scope.execution['complete_phase_observations'] if r['kind']=='native' and r['alias']=='beta'}
    records=json.loads((graph_root/'submitted/recordings.json').read_text())
    records={Path(r['input_uri']).parent.name:r for r in records}

    def case(label,category,argv,*,stdin=None,protected=(),expected_exit=1,exception=None,message=None,
             phase=None,publication=None,result=None,plugin_absent=False,details=None):
        directory=root/'cases'/label;directory.mkdir(exist_ok=True)
        cwd=directory/'execution';cwd.mkdir()
        publication=publication or directory/'publication';publication.mkdir(parents=True,exist_ok=True)
        value=dict(label=label,category=category,argv=[str(v) for v in argv],program_sha256=sha(argv[0]),
            cwd=str(cwd),journal=str(directory/'runtime-observation.json'),publication=str(publication),
            protected_files=sorted({str(Path(p)) for p in protected}),stdin_file=str(stdin) if stdin else None,
            expected=dict(exit=expected_exit,exception=exception,message=message,phase=phase,
                          result=str(result) if result else None,plugin_absent=plugin_absent),
            details=details or {},synthetic_counterexample=expected_exit!=0,original_producer_receipts_rewritten=False)
        path=directory/'scenario.json';write(path,value)
        scenarios.append(dict(label=label,path=str(path),sha256=sha(path)))
        return value

    def validator(label,row,mutate=None,projection_mutate=None,nwb=None):
        directory=root/'cases'/label;directory.mkdir()
        task=Path(row['task_workdir'])/'scientific-task'
        original=task/'checks-request.json';request=json.loads(original.read_text())
        protected=[original]
        if mutate:
            value=json.loads(Path(request['lindi']).read_text());before=copy.deepcopy(value)
            mutate(value)
            require(value!=before,'Actually changed sidecar payload '+label)
            path=directory/'control.lindi.json';write(path,value);request['lindi']=str(path);protected.append(path)
        if projection_mutate:
            value=json.loads(Path(request['projection']).read_text());before=copy.deepcopy(value)
            projection_mutate(value)
            require(value!=before,'Actually changed projection '+label)
            path=directory/'control.projection.json';write(path,value);request['projection']=str(path);protected.append(path)
        if nwb:request['nwb']=str(nwb);protected.append(nwb)
        path=directory/'request.json'
        if mutate or projection_mutate or nwb:write(path,request)
        else:path=original
        protected.append(path)
        return directory,path,protected

    # Reexecute all eight original request bytes, without rewriting paths/URLs.
    for row in scope.execution['complete_phase_observations']:
        label='positive-'+'-'.join(row[key] for key in ('kind','alias','case'))
        directory,request,protected=validator(label,row)
        case(label,'accepted-current-verifier',[verifier,request,directory/'checks.json'],protected=protected,
             expected_exit=0,result=directory/'checks.json',details=dict(original_request_sha256=sha(request),
             graph_kind=row['kind'],alias=row['alias'],layout=row['case']))

    for layout,row in native_rows.items():
        for field,index in [('offset',1),('length',2)]:
            for position in ('sampled','unsampled'):
                key=SIGNAL.lstrip('/')+('/0.0' if position=='sampled' else '/1.1')
                label='lindi-float-'+field+'-'+position+'-'+layout
                def change(value,key=key,index=index):value['refs'][key][index]=float(value['refs'][key][index])
                # Type equality is meaningful even though Python value equality is not.
                directory=root/'cases'/label;directory.mkdir()
                original=Path(row['task_workdir'])/'scientific-task/checks-request.json'
                request=json.loads(original.read_text());value=json.loads(Path(request['lindi']).read_text())
                before=value['refs'][key][index];change(value)
                require(type(before) is int and type(value['refs'][key][index]) is float and before==value['refs'][key][index],
                        'Real numerically equal floating range field')
                sidecar=directory/'control.lindi.json';write(sidecar,value);request['lindi']=str(sidecar)
                request_path=directory/'request.json';write(request_path,request)
                case(label,'malformed-sidecar',[verifier,request_path,directory/'checks.json'],protected=[original,request_path,sidecar],
                     exception='SystemExit' if position=='sampled' else 'ContractError',
                     message='LINDI chunk reference is missing or malformed' if position=='sampled' else 'Malformed LINDI signal byte reference',
                     phase='validate_outputs',result=directory/'checks.json',details=dict(changed_key=key,field=field,before=before,after=value['refs'][key][index]))

        # Skipping filter401 retains all neural values, while violating native ABI.
        label='nwb-skipped-codec-'+layout;directory=root/'cases'/label;directory.mkdir()
        copied=directory/'output.nwb';shutil.copy2(Path(row['task']['outputs']['nwb'].removeprefix('file://')),copied)
        with h5py.File(copied,'r+') as handle:
            dataset=handle[SIGNAL];truth=dataset[:];coordinate=(0,0)
            chunk=np.zeros(dataset.chunks,dtype=dataset.dtype)
            chunk[:min(dataset.shape[0],dataset.chunks[0]),:min(dataset.shape[1],dataset.chunks[1])]=truth[:dataset.chunks[0],:dataset.chunks[1]]
            mask,encoded=dataset.id.read_direct_chunk(coordinate)
            require(mask==0,'Original native chunk used filter401')
            dataset.id.write_direct_chunk(coordinate,chunk.tobytes(),filter_mask=1)
        # The writer's chunk cache can retain the previous mask. Reopen before
        # proving the persisted malformed codec and the unchanged neural values.
        with h5py.File(copied,'r') as handle:
            dataset=handle[SIGNAL]
            require(dataset.id.read_direct_chunk(coordinate)[0]==1 and dataset[:].tobytes()==truth.tobytes(),
                    'Actual skipped filter with every scientific sample unchanged')
        directory,request,protected=validator('verify-skipped-codec-'+layout,row,nwb=copied)
        case('verify-skipped-codec-'+layout,'skipped-codec',[verifier,request,directory/'checks.json'],protected=protected,
             exception='SystemExit',message='Maxwell filter 401 was skipped for an allocated HDF5 chunk',phase='validate_outputs',result=directory/'checks.json')
        case(label,'skipped-codec',[generator,'--nwb-file',copied,'--output-file',directory/'new.lindi.json',
             '--source-url',row['evidence']['reader_url']],protected=[copied],exception='SystemExit',
             message='Maxwell filter 401 was skipped for an allocated HDF5 chunk',phase='lindi',result=directory/'new.lindi.json')

    row=native_rows['maxone']
    mutations=[
        ('unknown-lindi-codec',lambda v:v['refs'][SIGNAL.lstrip('/')+'/.zarray']['filters'][0].update(id='unknown-runtime-codec'),'SystemExit','Maxwell filter declaration mismatch'),
        ('unknown-codec-version',lambda v:v['generationMetadata']['maxwellFilter401Codec'].update(codec_version=2),'SystemExit','unexpected Maxwell codec declaration'),
        ('missing-range',lambda v:v['refs'].pop(SIGNAL.lstrip('/')+'/1.1'),'ContractError','LINDI signal chunk coverage is incomplete or unexpected'),
        ('boolean-range-offset',lambda v:v['refs'][SIGNAL.lstrip('/')+'/1.1'].__setitem__(1,True),'ContractError','Malformed LINDI signal byte reference'),
        ('foreign-reader-url',lambda v:v['generationMetadata'].update(sourceUrl='file:///foreign/output.nwb'),'ContractError','LINDI reader URL differs from the reviewed durable output'),
    ]
    for label,mutate,error,message in mutations:
        directory,request,protected=validator(label,row,mutate=mutate)
        case(label,'malformed-sidecar',[verifier,request,directory/'checks.json'],protected=protected,
             exception=error,message=message,phase='validate_outputs',result=directory/'checks.json')
    projections=[
        ('wrong-modeled-value',lambda v:v['context'].update(temperature_c=38)),
        ('wrong-ordered-reference',lambda v:v['cultures']['recorded']['parent_culture_ids'].reverse()),
        ('partial-modeled-graph',lambda v:v['cultures'].pop('parent-a')),
    ]
    for label,mutate in projections:
        directory,request,protected=validator(label,row,projection_mutate=mutate)
        case(label,'projection-mismatch',[verifier,request,directory/'checks.json'],protected=protected,
             exception='ContractError',message='NWB modeled metadata differs from the frozen projection',phase='validate_outputs',result=directory/'checks.json')

    owner=copy.deepcopy(row['native_plan']['semantic']['owner'])
    base_env=os.environ.copy()
    def metadata_call(label,operation,request,directory):
        directory.mkdir();request_path=directory/'submitted.json';write(request_path,request)
        command=[sys.executable,str(native),operation]
        env=base_env.copy();env['KS4_TASK_OBSERVER_DIR']=str(directory/'task-observer')
        started=time.monotonic()
        with (directory/'cli.log').open('xb') as stream:
            child=subprocess.Popen(command,cwd=directory,stdin=request_path.open('rb'),stdout=stream,stderr=subprocess.STDOUT,env=env,start_new_session=True)
            try:code=child.wait(timeout=120)
            except BaseException:
                os.killpg(child.pid,signal.SIGKILL);child.wait();raise
        record=dict(label=label,operation=operation,argv=command,stdin_sha256=sha(request_path),exit=code,duration_s=time.monotonic()-started)
        write(directory/'actual-command.json',record);setup.append(record)
        require(code==0,'Actual owning metadata preparation failed: '+label+' '+operation)
        return directory

    def request_for(record,label):
        directory=root/'cases'/label;directory.mkdir(exist_ok=True)
        publication=directory/'publication';publication.mkdir()
        context=dict(run_id='runtime-'+label,step_id='raw',workflow_version=owner['workflow_version'],
            output_base_uri=publication.as_uri()+'/',collection_uri=publication.as_uri()+'/derived/nwb/',runtime={})
        settings=dict(nwb_chunk_duration_ms=10,create_linked_maxwell_companion=False)
        value=dict(meta=dict(owner=owner,transport='local',runtime={},context=context,settings=settings),
                   recordings=[record],resources=dict(cpus=4,memory='8 GiB',scope='local runtime control'),attempt=1)
        return directory,publication,value

    def native_failure(label,record,*,unit=False,error,message,phase,exit=1,plugin_absent=False,protected=(),details=None):
        directory,publication,request=request_for(record,label)
        if unit:
            raw=metadata_call(label,'raw',request,directory/'setup-raw')
            request['meta']['context']['step_id']='conversion';request['artifacts']=(raw/'artifact-uri.txt').read_text().strip()
            planned=metadata_call(label,'prepare',request,directory/'setup-prepare')
            plan=planned/'conversion-plan.json';data=json.loads(plan.read_text());task,=data['tasks']
            request['meta']['unit_id']=task['unit']['unit_id'];arguments=[native,'unit','--plan-file',plan]
            protected=[*protected,plan]
        else:arguments=[native,'raw']
        submitted=directory/'submitted.json';write(submitted,request)
        case(label,'native-unit' if unit else 'native-raw',arguments,stdin=submitted,protected=[submitted,*protected],
             expected_exit=exit,exception=error,message=message,phase=phase,publication=publication,
             plugin_absent=plugin_absent,details=details)

    def raw_control(layout,label,kind):
        directory=root/'private-inputs'/label;directory.mkdir(parents=True)
        original=graph_root/'inputs'/layout/'recording.raw.h5';target=directory/'recording.raw.h5';shutil.copy2(original,target)
        facts=scope.read(graph_root/'inputs'/layout/'facts.json');selected,=[value for value in facts['stores'] if value['selected']]
        path=selected['raw_dataset_path'];details=dict(original_sha256=sha(original),control_kind=kind,layout=layout)
        if kind=='not-hdf5':target.write_bytes(b'Explicit malformed Maxwell HDF5 signature\n')
        else:
            with h5py.File(target,'r+') as handle:
                if kind=='missing-mapping':del handle[selected['store']+'/settings/mapping']
                else:
                    old=handle[path];shape=old.shape;chunks=old.chunks;dtype=old.dtype;attrs=dict(old.attrs)
                    encoded=[]
                    def collect(info):
                        coordinate=tuple(info.chunk_offset);mask,payload=old.id.read_direct_chunk(coordinate)
                        require(mask==0,'Native source codec before unknown-filter control');encoded.append((coordinate,payload))
                    if kind=='unknown-filter':old.id.chunk_iter(collect)
                    links=[];visited=set()
                    def walk(group):
                        if group.id in visited:return
                        visited.add(group.id)
                        for name in group:
                            item=group[name]
                            if isinstance(item,h5py.Dataset) and item.id==old.id:links.append((group.name,name))
                            elif isinstance(item,h5py.Group):walk(item)
                    walk(handle['/'])
                    require(links,'All direct source raw aliases located')
                    for parent,name in links:del handle[parent][name]
                    if kind=='wrong-shape':new=handle.create_dataset(path,data=np.arange(4,dtype='<u2'))
                    else:
                        require(kind=='unknown-filter' and not h5py.h5z.filter_avail(32031),'Actual unavailable control codec')
                        props=h5py.h5p.create(h5py.h5p.DATASET_CREATE);props.set_chunk(chunks);props.set_filter(32031,h5py.h5z.FLAG_OPTIONAL)
                        parent,name=path.rsplit('/',1)
                        identifier=h5py.h5d.create(handle[parent].id,name.encode(),h5py.h5t.py_create(dtype),h5py.h5s.create_simple(shape),dcpl=props)
                        new=h5py.Dataset(identifier)
                        for coordinate,payload in encoded:new.id.write_direct_chunk(coordinate,payload,filter_mask=0)
                        details['encoded_chunks']=[dict(coordinate=list(c),sha256=hashlib.sha256(p).hexdigest()) for c,p in encoded]
                    for key,value in attrs.items():new.attrs[key]=value
                    for parent,name in links:
                        if parent.rstrip('/')+'/'+name!=path:handle[parent][name]=new
        require(sha(target)!=sha(original),'Real malformed input bytes')
        details['control_sha256']=sha(target)
        record=copy.deepcopy(records[layout]);record['input_uri']=target.as_uri();record.pop('source_reference',None)
        return record,target,details

    for layout in native_rows:
        label='native-unknown-codec-'+layout;record,target,details=raw_control(layout,label,'unknown-filter')
        native_failure(label,record,unit=True,error='ScientificFailure',message='Guarded phase failed: validate_input (exit 1)',
                       phase='validate_input',protected=[target],details=details)
        label='native-missing-mapping-'+layout;record,target,details=raw_control(layout,label,'missing-mapping')
        native_failure(label,record,unit=True,error='ScientificFailure',message='Guarded phase failed: convert (exit 1)',
                       phase='convert',protected=[target],details=details)
    native_failure('native-missing-plugin',copy.deepcopy(records['maxone']),unit=True,error='RuntimeError',
        message='Maxwell HDF5 filter 401 plugin is not bundled in this runtime image; runtime downloads are disabled',
        phase='owner_runtime',plugin_absent=True)
    # LocalStore retains this HDF5 OSError as the context of a terminal I/O76.
    for kind,error,message,exit in [('not-hdf5','OSError','file signature not found',76),
                                   ('wrong-shape','HeaderError','Expected a nonempty two-dimensional numeric Maxwell raw table',2)]:
        label='native-'+kind;record,target,details=raw_control('maxone',label,kind)
        native_failure(label,record,error=error,message=message,phase='raw_header',exit=exit,protected=[target],details=details)

    from ephys_modular.local import LocalStore
    record=copy.deepcopy(records['maxone']);actual=LocalStore().stat(record['input_uri']);actual['sha256']='0'*64;actual['etag']='sha256-'+'0'*64
    record['source_reference']=actual
    native_failure('native-wrong-source-digest',record,error='IntegrityError',message='Stored local object identity changed',phase='raw_source_identity',exit=76)
    record=copy.deepcopy(records['maxone']);record['nwb_metadata']['recording']['sha256']='0'*64;record['nwb_metadata']['recording']['etag']='sha256-'+'0'*64
    native_failure('native-wrong-projection-digest',record,error='IntegrityError',message='Stored local object identity changed',phase='raw_projection_identity',exit=76)
    for label,mutate,error,message in [
        ('native-partial-projection',lambda v:v['cell_lines']['source-line']['genetic_variants'][0].pop('origin'),'ValueError','origin requires author input'),
        ('native-partial-graph',lambda v:v['cultures'].pop('parent-a'),'ValueError','Invalid or cyclic cultured preparation reference: parent-a'),
    ]:
        directory=root/'private-inputs'/label;directory.mkdir()
        record=copy.deepcopy(records['maxone']);old=Path(record['nwb_metadata']['recording']['uri'].removeprefix('file://'))
        value=json.loads(old.read_text());mutate(value);path=directory/'projection.json';write(path,value)
        record['nwb_metadata']['recording']=dict(uri=path.as_uri(),sha256=sha(path))
        native_failure(label,record,error=error,message=message,phase='raw_projection_model',protected=[path])
    write(root/'prepared.json',dict(passed=True,graph_root=str(graph_root),graph_seal=GRAPH_SEAL,
        scenarios=scenarios,setup_invocations=setup,owning_native_source=graph.SOURCE,owning_version=graph.VERSION,
        owning_image=graph.IMAGE,original_receipts_or_scientific_files_modified=False,
        positive_cases=sum(item['label'].startswith('positive-') for item in scenarios),
        total_cases=len(scenarios)))
    print(json.dumps(dict(prepared=len(scenarios),positive_cases=8,actual_metadata_setup_calls=len(setup))),flush=True)


def check(root):
    import current_graph as graph
    from inputs import sha,typed
    from canonical import require
    prepared=json.loads((root/'prepared.json').read_text());results=[]
    graph.CurrentGraphScope(Path(prepared['graph_root']),prepared['graph_seal'])
    for item in prepared['scenarios']:
        path=Path(item['path']);require(sha(path)==item['sha256'],'Frozen runtime scenario')
        scenario=json.loads(path.read_text());expected=scenario['expected']
        terminal=json.loads((root/'observed'/item['label']/'terminal.json').read_text())
        journal=json.loads(Path(scenario['journal']).read_text())
        require(terminal['actual_state']['ExitCode']==expected['exit']==journal['observed_exit'] and
                not terminal['actual_state']['Running'] and not terminal['actual_state']['OOMKilled'],'Actual expected worker exit '+item['label'])
        require(journal['journal_initialized'] and journal['manifest_sha256']==item['sha256'] and
                journal['program_sha256']==journal['program_sha256_after']==scenario['program_sha256'], 'Actual program/journal binding')
        require(journal['protected_before']==journal['protected_after'],'Runtime control inputs unchanged')
        require(journal['publication_before']==journal['publication_after'],'No new scientific publication after rejection/verification')
        require(terminal['network']=='none' and terminal['actual_image_id']=='sha256:d3ddc444a591f595f86c0ae4d7f573fd849f30c1f6cf8b84d1b4f81c51d2c388',
                'Isolated pinned actual worker image')
        require(journal['uid']==journal['gid']==0 and terminal['default_user'] in ('','root','0',None),'Image default user preserved')
        logs=(root/'observed'/item['label']/'worker.log').read_text()
        if expected['exit']==0:
            result=json.loads(Path(expected['result']).read_text())
            original_checks=Path(scenario['argv'][1]).with_name('checks.json')
            require(journal['normal_return'] and not journal['exceptions'] and result['complete'] is True and
                    result['all_signal_values_equal'] and result['all_lindi_signal_ranges_equal'] and result['modeled_projection_verified'],
                    'Unchanged current scientific verifier positive')
            require(typed(result)==typed(json.loads(original_checks.read_text())), 'Every original positive check field and JSON type')
        else:
            matching=[value for value in journal['exceptions'] if value['type']==expected['exception']]
            require(not journal['normal_return'] and matching and
                    (any(expected['message'] in value['message'] for value in matching) or
                     (expected['exception']=='SystemExit' and expected['message'] in logs)),
                    'Intended exception, not an unrelated failure: '+item['label'])
            phase_function=dict(validate_input='_run',convert='_run',validate_outputs='verify_outputs',lindi='main',
                owner_runtime='validate_owner_runtime',raw_header='inspect_input',raw_source_identity='_freeze_source',
                raw_projection_identity='_freeze_projection',raw_projection_model='_projection_metadata_size')[expected['phase']]
            require(any(frame['function']==phase_function for value in journal['exceptions'] for frame in value['frames']),
                    'Actual owning failure boundary '+item['label'])
            if expected['result']:require(not Path(expected['result']).exists(),'No fabricated success result')
            if scenario['category']=='native-unit' and expected['phase'] in ('validate_input','convert'):
                require('[scientific_phase] '+expected['phase']+' failed (exit 1)' in logs,'Actual owning scientific phase failure')
                phase_log=Path(scenario['cwd'])/'scientific-task'/(expected['phase']+'.log')
                text=phase_log.read_text()
                if expected['phase']=='validate_input':
                    plan=json.loads(Path(scenario['argv'][scenario['argv'].index('--plan-file')+1]).read_text())
                    task,=plan['tasks']
                    require(task['header']['header']['selected'][0]['filter_ids']==[32031] and
                            'Maxwell compressed source data is unreadable' in text,
                            'Actual planned unknown codec reaches full-read rejection')
                else:require('mapping' in text,'Missing mapping reaches the original converter')
                observed=list((Path(scenario['cwd'])/'task-observer').glob('*.start.json'))
                names=[Path(json.loads(path.read_text())['program']).name for path in observed]
                require('validate_maxwell_input.py' in names and
                        (expected['phase']!='convert' or 'convert_maxone_to_nwb.py' in names),
                        'Actual scientific child startup journals are mandatory')
                for path in observed:
                    require(path.with_name(path.name.replace('.start.json','.end.json')).is_file(),
                            'Complete original scientific child observation')
            require(not (Path(scenario['cwd'])/'native-task-evidence.json').exists(),'No successful native evidence after failure')
            require(not list(Path(scenario['cwd']).glob('outcome-*.json')) and
                    not (Path(scenario['cwd'])/'artifact-uri.txt').exists() and
                    not (Path(scenario['cwd'])/'result.txt').exists(),'No fabricated completed artifact port')
        if expected['plugin_absent']:
            require(all(not value['exists'] for value in journal['plugin_files'].values()),'Plugin really unavailable in the worker')
        results.append(dict(label=item['label'],passed=True,actual_exit=expected['exit'],expected_phase=expected['phase'],
            actual_exceptions=journal['exceptions'],publication_unchanged=True,protected_inputs_unchanged=True))
    result=dict(passed=True,total_cases=len(results),positive_cases=prepared['positive_cases'],negative_cases=len(results)-prepared['positive_cases'],
                actual_metadata_setup_calls=len(prepared['setup_invocations']),cases=results,
                current_graph_evidence_unchanged=True,original_acceptance_question_pending=True,
                parity_parent_complete=False,full_goal_status='active')
    write(root/'reports/runtime-controls.json',result)
    print(json.dumps({key:value for key,value in result.items() if key!='cases'}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('operation',choices=('prepare','check'))
    parser.add_argument('--root',required=True,type=Path);parser.add_argument('--graph-root',type=Path)
    args=parser.parse_args()
    if args.operation=='prepare':prepare(args.root,args.graph_root)
    else:check(args.root)
