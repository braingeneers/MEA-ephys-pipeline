#!/usr/bin/env python3
"""Generate and check real scientific counterexamples in the pinned oracle runtime.

Prepare new copied trees, retain each changed tree, then restore its workspace
byte-for-byte. Check all trees read-only in a separate invocation. Synthetic
counterexamples are never successful producer receipts or workflow executions.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import resource
import sys
import time
import traceback
import uuid
import warnings

import h5py
import numpy as np
from pynwb import validate
import artifacts
import canonical
from canonical import RefArray, require
from inputs import sha, typed


def read(path):
    return json.loads(path.read_text())


def write_new(path, value):
    with path.open('x') as stream:
        stream.write(json.dumps(value, indent=2) + '\n')


def inventory(root):
    result = {}
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink(), 'Control trees cannot contain symlinks')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = dict(bytes=path.stat().st_size, sha256=sha(path))
    return result


def replace_dataset(handle, path, values, dtype=None):
    old = handle[path]
    attributes = []
    for name in old.attrs:
        attr = old.attrs.get_id(name)
        try:attributes.append((name,old.attrs[name],attr.dtype))
        finally:attr.close()
    dtype = old.dtype if dtype is None else dtype
    del handle[path]
    new = handle.create_dataset(path, data=values, dtype=dtype)
    for name, value, attr_dtype in attributes:
        new.attrs.create(name, value, dtype=attr_dtype)
    return new


def expected_nodes(tree, case):
    return canonical.expected_graph(tree/'inputs'/case, read(tree/'inputs'/case/'facts.json'),
                                    read(tree/'receipts'/(case+'.json')))[0]


def definitions(tree):
    """Define targets through input-only canonical rules, not produced values."""
    nodes = expected_nodes(tree, 'maxtwo_routed')
    tests = []
    def add(name, kind, expect, **settings):
        tests.append(dict(id=name, kind=kind, expected_error=expect, case='maxtwo_routed', mode='enabled', **settings))
    for key, node in nodes.items():
        if node.payload is not None:
            expect=key+': unique owned child' if key=='lines/derived-line/@parent_cell_line' else key+'.data'
            add('Data-'+key.replace('/','_').replace('@',''), 'Data', expect, key=key)
    require(sum(x['kind']=='Data' for x in tests)==13, 'All thirteen HDMF Data payloads need controls')
    for case in ['maxone','maxtwo_routed']:
        for position in ['first','last']:
            tests.append(dict(id=case+'-signal-'+position, kind='signal-value', case=case, mode='enabled',
                              position=position, expected_error='signal.data: exact array values'))
    for field, path in [('starting_time','/acquisition/ElectricalSeries/starting_time'),
                        ('rate','/acquisition/ElectricalSeries/starting_time'),
                        ('conversion','/acquisition/ElectricalSeries/data'),
                        ('offset','/acquisition/ElectricalSeries/data')]:
        add('signal-'+field,'clock-calibration','signal.'+field+': exact scalar float/type', field=field,path=path)
    add('signal-dtype','signal-dtype-shape','signal.data: dtype/shape', action='dtype')
    add('signal-shape','signal-dtype-shape','signal.data: dtype/shape', action='shape')
    facts=read(tree/'inputs/maxtwo_routed/facts.json')
    for key in facts['model']['numeric_storage_normalization']:
        owner,field=key.rsplit('/',1)
        add('scalar-'+key.replace('/','_'),'model-scalar',owner+'.'+field+': exact scalar float/type',key=owner,field=field)
    for owner,field,value in [('cultures/parent-b','age','P76D'),
                              ('lines/source-line/genetic_variants/variant-one','target_coordinates','chr1:202-203'),
                              ('context','optical_stimulation',True)]:
        add('model-'+owner.replace('/','_')+'-'+field,'model-value',owner+'.'+field+': exact scalar value/type',
            key=owner,field=field,value=value)
    add('schema-float32-storage','model-storage','/general/experiment_context@temperature_c: stored float dtype')
    add('negative-zero-model','model-zero','pharmacologies/vehicle.start_time_s: exact scalar float/type')
    add('duplicate-parent-reference','duplicate-parent','cultures/recorded/@parent_cultures.data[1]: canonical object identity')
    add('truncated-parent-reference','truncate-parent','cultures/recorded/@parent_cultures.data: reference shape/cardinality')
    add('wrong-recorded-culture-alias','alias-target','subject.culture: canonical object identity')
    add('independent-device-alias','alias-copy','context: complete field inventory')
    add('collapsed-authored-parents','collapse-parents','cultures/parent-b: unique owned child')
    add('valid-UUID-swapped-parent-ages','swap-parent-ages','cultures/parent-a.age: exact scalar value/type')
    for filename in ['recordings.json','bindings.json']:
        for action in ['integer','floating','signed_zero','large_integer','negative_integer','bool','null','unicode','keep','omitted']:
            add(filename[:-5]+'-'+action,'transport','Original transport JSON types, values, signs and unknown fields',
                filename=filename,action=action)
    require(len({x['id'] for x in tests})==len(tests),'Unique control IDs')
    return tests


def reference_value(handle, value):
    target=handle[value]
    return dict(path=target.name,object_id=str(target.attrs.get('object_id')),kind=str(target.attrs.get('neurodata_type')))


def mutate(tree, test):
    case=test['case'];nodes=expected_nodes(tree,case);kind=test['kind']
    nwb=tree/'artifacts'/case/'output.nwb'
    if kind=='transport':
        path=tree/'inputs'/test['filename'];value=read(path)
        def find(obj):
            if isinstance(obj,dict):
                if 'unknown_typed' in obj:return obj['unknown_typed']
                for child in obj.values():
                    found=find(child)
                    if found is not None:return found
            elif isinstance(obj,list):
                for child in obj:
                    found=find(child)
                    if found is not None:return found
            return None
        target=find(value);require(target is not None,'Original unknown transport metadata')
        before=typed(target);action=test['action']
        if action=='integer':target[action]=float(target[action])
        elif action=='floating':target[action]=int(target[action])
        elif action=='signed_zero':target[action]=0.0
        elif action in ['large_integer','negative_integer']:target[action]=int(float(target[action]))
        elif action=='bool':target[action]=0
        elif action=='null':target[action]=''
        elif action=='unicode':target[action]='mV'
        elif action=='keep':target[action][-1]=2.0
        else:del target['keep']
        after=typed(target);require(before!=after,'Real typed transport mutation')
        path.write_text(json.dumps(value,indent=2)+'\n')
        return dict(before=before,after=after,unchanged_scientific_NWB=True)
    with h5py.File(nwb,'r+') as handle:
        if kind=='Data':
            key=test['key'];node=nodes[key];dataset=handle[node.path];value=dataset[:]
            before_shape=list(value.shape)
            if isinstance(node.payload,RefArray):
                before=[reference_value(handle,x) for x in value]
                if key=='electrodes/group':
                    value[0]=handle[nodes['device'].path].ref
                elif key=='lines/derived-line/@parent_cell_line':
                    # The schema fixes a present vector at one CellLine, and the
                    # other existing line would be a self-parent. Optional absence
                    # remains schema-valid but contradicts the authored Data contract.
                    del handle[node.path]
                    return dict(path=node.path,before=before,after=None,shape_before=before_shape,shape_after=None,
                                authored_Data_removed=True,schema_optional_field_preserved=True)
                elif key.startswith('cultures/parent-'):
                    alternate=next(k for k in ['lines/source-line','lines/derived-line'] if k!=node.payload.keys[0])
                    value[0]=handle[nodes[alternate].path].ref
                else:value[:]=value[::-1]
                dataset[:]=value
                after=[reference_value(handle,x) for x in dataset[:]]
            elif value.dtype.kind in 'iu':
                before=typed(value[0].item());value[0]+=1 if key=='signal/electrodes' else 101;dataset[:]=value;after=typed(dataset[0].item())
            elif value.dtype.kind=='f':
                before=typed(value[0].item());value[0]=np.nextafter(value[0],np.inf);dataset[:]=value;after=typed(dataset[0].item())
            else:
                before=repr(dataset[0]);dataset[0]='deliberately wrong value';after=repr(dataset[0])
            require(before!=after,'Real Data payload mutation')
            return dict(path=node.path,before=before,after=after,shape_before=before_shape,shape_after=list(dataset.shape))
        if kind=='signal-value':
            dataset=handle['/acquisition/ElectricalSeries/data'];index=(0,0) if test['position']=='first' else (dataset.shape[0]-1,dataset.shape[1]-1)
            before=int(dataset[index]);dataset[index]=np.uint16(before^1);after=int(dataset[index]);require(before!=after,'Real sample mutation')
            return dict(path=dataset.name,index=list(index),before=before,after=after)
        if kind=='clock-calibration':
            obj=handle[test['path']];field=test['field']
            if field=='starting_time':
                before=float(obj[()]);obj[()]=np.float64(before+1/20000);after=float(obj[()])
            else:
                before=float(obj.attrs[field]);obj.attrs.modify(field,np.float64(before*1.001 if before else 1e-9));after=float(obj.attrs[field])
            require(before.hex()!=after.hex(),'Real clock/calibration mutation')
            return dict(path=obj.name,field=field,before_hex=before.hex(),after_hex=after.hex())
        if kind=='signal-dtype-shape':
            path='/acquisition/ElectricalSeries/data';old=handle[path];value=old[:];before=dict(dtype=value.dtype.str,shape=list(value.shape))
            value=value.astype('<u4') if test['action']=='dtype' else value.T
            new=replace_dataset(handle,path,value,value.dtype)
            return dict(path=path,before=before,after=dict(dtype=new.dtype.str,shape=list(new.shape)))
        if kind in ['model-scalar','model-value']:
            obj=handle[nodes[test['key']].path];field=test['field'];before=obj.attrs[field]
            value=np.nextafter(np.float64(before),np.inf) if kind=='model-scalar' else test['value']
            obj.attrs.modify(field,value);after=obj.attrs[field]
            before=typed(before.item() if isinstance(before,np.generic) else before)
            after=typed(after.item() if isinstance(after,np.generic) else after)
            require(before!=after,'Real authored model scalar mutation')
            return dict(path=obj.name,field=field,before=before,after=after)
        if kind=='model-storage':
            obj=handle[nodes['context'].path];before=obj.attrs['temperature_c'];del obj.attrs['temperature_c'];obj.attrs.create('temperature_c',np.float32(before))
            require(float(before).hex()==float(obj.attrs['temperature_c']).hex(),'Isolate storage dtype without changing scalar value')
            return dict(path=obj.name,field='temperature_c',before_dtype='<f8',after_dtype=np.asarray(obj.attrs['temperature_c']).dtype.str,value_hex=float(before).hex())
        if kind=='model-zero':
            obj=handle[nodes['pharmacologies/vehicle'].path];before=float(obj.attrs['start_time_s']);obj.attrs.modify('start_time_s',np.float64(-0.0));after=float(obj.attrs['start_time_s'])
            require(before.hex()!=after.hex(),'Real scalar zero-sign mutation')
            return dict(path=obj.name,field='start_time_s',before_hex=before.hex(),after_hex=after.hex())
        if kind in ['duplicate-parent','truncate-parent']:
            path=nodes['cultures/recorded/@parent_cultures'].path;dataset=handle[path];values=dataset[:]
            before=[reference_value(handle,x) for x in values]
            require(handle[values[0]].attrs['culture_id']==handle[values[1]].attrs['culture_id'] and handle[values[0]].id!=handle[values[1]].id,'Same biological ID with distinct authored parents')
            if kind=='duplicate-parent':dataset[1]=values[0]
            else:dataset=replace_dataset(handle,path,values[:1])
            return dict(path=path,before=before,after=[reference_value(handle,x) for x in dataset[:]],same_biological_id='shared-parent')
        if kind=='alias-target':
            path='/general/subject/culture';before=reference_value(handle,handle[path].ref)
            require(handle[path].id==handle[nodes['cultures/recorded'].path].id,'Original alias identity')
            del handle[path];handle[path]=h5py.SoftLink(nodes['cultures/parent-a'].path)
            require(handle[path].id!=handle[nodes['cultures/recorded'].path].id,'Real alias target mutation')
            return dict(path=path,before=before,after=reference_value(handle,handle[path].ref))
        if kind=='alias-copy':
            path='/general/experiment_context/device';target=nodes['device'].path;before=reference_value(handle,handle[path].ref)
            require(handle[path].id==handle[target].id,'Original shared device alias')
            del handle[path];handle.copy(target,path);handle[path].attrs.modify('object_id',str(uuid.uuid4()))
            require(handle[path].id!=handle[target].id,'Independent physical object with identical device science')
            return dict(path=path,before=before,after=reference_value(handle,handle[path].ref),original_alias_shared_storage=True,changed_alias_shared_storage=False)
        if kind=='collapse-parents':
            left=nodes['cultures/parent-a'].path;right=nodes['cultures/parent-b'].path
            require(handle[left].id!=handle[right].id and handle[left].attrs['culture_id']==handle[right].attrs['culture_id'],'Distinct original authored parents')
            before=reference_value(handle,handle[right].ref)
            handle[nodes['cultures/recorded/@parent_cultures'].path][1]=handle[left].ref
            del handle[right];handle[right]=handle[left]
            require(handle[left].id==handle[right].id,'Real physical parent collapse')
            return dict(path=right,before=before,after=reference_value(handle,handle[right].ref),physical_canonical_nodes_collapsed=True)
        if kind=='swap-parent-ages':
            left=handle[nodes['cultures/parent-a'].path];right=handle[nodes['cultures/parent-b'].path]
            before=[str(left.attrs['age']),str(right.attrs['age'])];identifiers=[str(left.attrs['object_id']),str(right.attrs['object_id'])]
            require(before[0]!=before[1] and len(set(identifiers))==2 and all(uuid.UUID(x).version==4 for x in identifiers),'Distinct valid generated UUIDs with different authored ages')
            require(left.attrs['culture_id']==right.attrs['culture_id']=='shared-parent','Same biological parent ID')
            left.attrs.modify('age',before[1]);right.attrs.modify('age',before[0])
            return dict(before=before,after=[str(left.attrs['age']),str(right.attrs['age'])],unchanged_valid_distinct_UUIDs=identifiers,same_biological_id='shared-parent')
        raise ValueError('Unsupported control kind: '+kind)


def prepare(inputs, positives, reference, output):
    sys.path.insert(0,'/opt/ephys-modular/maxwell/scripts')
    from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
    configure_bundled_maxwell_hdf5_plugin()
    require(not output.exists(),'Control preparation requires a new output directory')
    output.mkdir();baselines=output/'baselines';baselines.mkdir();work=output/'restored';work.mkdir();cases=output/'cases';cases.mkdir()
    before_sources=dict(inputs=inventory(inputs),positives=inventory(positives),reference=sha(reference))
    baseline_records={}
    for mode in ['enabled','disabled']:
        target=baselines/mode;target.mkdir();shutil.copytree(inputs,target/'inputs');shutil.copytree(positives/mode/'artifacts',target/'artifacts');shutil.copytree(positives/mode/'receipts',target/'receipts');shutil.copy2(reference,target/'schema-reference.nwb')
        baseline_records[mode]=inventory(target)
    tests=definitions(baselines/'enabled');records=[]
    for test in tests:
        baseline=baselines/test['mode'];workspace=work/test['id'];shutil.copytree(baseline,workspace)
        evidence=mutate(workspace,test);mutated=inventory(workspace);original=baseline_records[test['mode']]
        require(set(mutated)==set(original),'A counterexample must keep the complete fixture file inventory')
        changed=[k for k in mutated if mutated[k]!=original[k]];require(bool(changed),'At least one real byte mutation required')
        require(not any(k.endswith('/facts.json') or k.endswith('transport-facts.json') or k.endswith('input-set.json') for k in changed),'Frozen scientific facts cannot be mutated')
        retained=cases/test['id'];shutil.copytree(workspace,retained)
        for name in changed:shutil.copy2(baseline/name,workspace/name)
        require(inventory(workspace)==original,'Every control workspace must be restored byte-for-byte')
        records.append(dict(test,mutation=evidence,changed_files={k:dict(original=original[k],mutated=mutated[k],restored=original[k]) for k in changed},mutated_inventory=mutated,restored_inventory=inventory(workspace)))
        print(json.dumps(dict(prepared=test['id'],changed=changed)),flush=True)
    require(before_sources==dict(inputs=inventory(inputs),positives=inventory(positives),reference=sha(reference)),'Original frozen source trees remain unchanged')
    result=dict(schema_version=1,scope='scientific_model_and_typed_JSON_controls',source_inputs=before_sources,baselines=baseline_records,controls=records,production_receipts_relabelled=False)
    write_new(output/'controls.json',result);return dict(passed=True,prepared=len(records))


def readonly_mounts():
    mounts={}
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields=line.split()
        if fields[4] in ['/code','/controls']:mounts[fields[4]]=fields[5].split(',')
    require(set(mounts)=={'/code','/controls'} and all('ro' in x for x in mounts.values()),'Code and all scientific control trees must be read-only')
    require(not Path('/evidence/positive').exists() and not Path('/evidence/published').exists() and not Path('/workflow-repo').exists(),'Producer work and checkout must be unavailable')
    return mounts


def check_one(controls, identifier, report):
    readonly_mounts();manifest=read(controls/'controls.json')
    test,=[x for x in manifest['controls'] if x['id']==identifier]
    sys.path.insert(0,'/opt/ephys-modular/maxwell/scripts')
    from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
    configure_bundled_maxwell_hdf5_plugin()
    start=time.monotonic();tree=controls/'cases'/test['id'];restored=controls/'restored'/test['id']
    require(inventory(tree)==test['mutated_inventory'],'Exact retained counterexample bytes')
    require(inventory(restored)==test['restored_inventory']==manifest['baselines'][test['mode']],'Exact byte restoration including facts and receipts')
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter('always')
        schema_errors=[str(x) for x in validate(path=str(tree/'artifacts'/test['case']/'output.nwb'))]
        failure=None
        try:artifacts.verify(tree/'inputs',tree/'artifacts',tree/'receipts',tree/'schema-reference.nwb')
        except Exception as exc:
            failure=dict(type=type(exc).__name__,message=str(exc),traceback=traceback.format_exc(),frames=[dict(file=Path(x.filename).name,function=x.name,line=x.lineno) for x in traceback.extract_tb(exc.__traceback__)])
    expected=bool(failure and failure['type']=='ValueError' and test['expected_error'] in failure['message'] and any(x['file'] in ['canonical.py','storage.py','artifacts.py'] for x in failure['frames']))
    item=dict(id=test['id'],kind=test['kind'],case=test['case'],mode=test['mode'],expected_error=test['expected_error'],failure=failure,schema_valid=not schema_errors,schema_errors=schema_errors,passed=expected and not schema_errors,
              changed_files=test['changed_files'],mutation=test['mutation'],restored_byte_identical=True,original_facts_unchanged=True,duration_s=time.monotonic()-start,warnings=[str(x.message) for x in captured])
    item['peak_rss_kib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    write_new(report,item)
    print(json.dumps({k:item[k] for k in ['id','passed','schema_valid','duration_s']}|dict(error=None if failure is None else failure['message'])),flush=True)
    return item


def check(controls, reports):
    require(sys.version_info[:2]==(3,11),'Pinned Python3.11 required');mounts=readonly_mounts()
    require(reports.is_dir() and not (reports/'controls.json').exists(),'New report destination required')
    manifest=read(controls/'controls.json');before=inventory(controls);positive=[];results=[]
    for mode in ['enabled','disabled']:
        tree=controls/'baselines'/mode;require(inventory(tree)==manifest['baselines'][mode],'Exact baseline copies')
        result=artifacts.verify(tree/'inputs',tree/'artifacts',tree/'receipts',tree/'schema-reference.nwb')
        require(result['passed'],'Unchanged complete positive');write_new(reports/('positive-'+mode+'.json'),result);positive.append(dict(mode=mode,cases=len(result['cases']),passed=True))
    def run_test(test):
        report=reports/(test['id']+'.json')
        command=[sys.executable,str(Path(__file__).resolve()),'one','--controls',str(controls),'--id',test['id'],'--report',str(report)]
        start=time.monotonic()
        with (reports/(test['id']+'-stdout.log')).open('x') as stream:
            try:
                process=subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,timeout=90)
                actual_exit=process.returncode
            except subprocess.TimeoutExpired:actual_exit='timeout90s'
        if report.exists():
            item=read(report)
            require((actual_exit==0)==item['passed'],'Worker exit must agree with its actual result')
        else:
            item=dict(id=test['id'],kind=test['kind'],case=test['case'],mode=test['mode'],passed=False,schema_valid=False,failure=dict(type='WorkerFailure',message=str(actual_exit)),duration_s=time.monotonic()-start)
            write_new(report,item)
        item['actual_worker_command']=command;item['actual_worker_exit']=actual_exit
        write_new(reports/(test['id']+'-worker.json'),dict(command=command,actual_exit=actual_exit,duration_s=time.monotonic()-start))
        return item
    with ThreadPoolExecutor(max_workers=2) as executor:
        for item in executor.map(run_test,manifest['controls']):
            results.append(item)
            print(json.dumps({k:item[k] for k in ['id','passed','schema_valid','duration_s']}|dict(error=None if item.get('failure') is None else item['failure']['message'])),flush=True)
    require(inventory(controls)==before,'Checking cannot change retained controls, baselines, restored bytes or facts')
    import runtime_identity
    result=dict(passed=all(x['passed'] for x in results),scope=manifest['scope'],positive=positive,controls=results,total_controls=len(results),read_only_mounts=mounts,actual_uid=os.getuid(),actual_gid=os.getgid(),runtime_identity=runtime_identity.identity(),
                all_inputs_artifacts_receipts_facts_unchanged_during_check=True,all_restored_byte_identical=True,full_oracle_parent_complete=False,actual_workflow_graphs_claimed=False,generic_client_compatibility_claimed=False)
    write_new(reports/'controls.json',result);return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='action',required=True)
    p=commands.add_parser('prepare')
    for name in ['inputs','positives','reference','output']:p.add_argument('--'+name,required=True,type=Path)
    p=commands.add_parser('check')
    for name in ['controls','reports']:p.add_argument('--'+name,required=True,type=Path)
    p=commands.add_parser('one',help=argparse.SUPPRESS)
    p.add_argument('--controls',required=True,type=Path);p.add_argument('--id',required=True);p.add_argument('--report',required=True,type=Path)
    args=parser.parse_args()
    if args.action=='prepare':result=prepare(args.inputs,args.positives,args.reference,args.output)
    elif args.action=='check':result=check(args.controls,args.reports)
    else:result=check_one(args.controls,args.id,args.report)
    print(json.dumps({k:v for k,v in result.items() if k not in ['controls','runtime_identity']},indent=2))
    if not result['passed']:raise SystemExit(1)
