#!/usr/bin/env python3
"""Prove physical-artifact guards using retained synthetic counterexamples.

Original scientific facts and producer receipts remain immutable. Any resealed
receipt belongs only to its labelled negative tree, never to producer evidence.
The accepted scientific-control module is reused unchanged for utility functions.
"""
from __future__ import annotations
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import time
import traceback
import warnings
import h5py
import numpy as np
import numcodecs
from pynwb import validate
import artifacts
import canonical
import controls
import storage
from canonical import require
from controls import read,write_new,inventory
from inputs import sha,typed

SIGNAL='/acquisition/ElectricalSeries/data'
SIDE_SIGNAL=SIGNAL.lstrip('/')


def definitions():
    rows=[
      ('native-chunk-dimensions','chunks','Input settings determine native chunk dimensions'),
      ('native-filter-pipeline','filter','Complete native filter pipeline'),
      ('native-skipped-filter-mask','mask','Every native chunk must use filter401'),
      ('native-padding-maxtwo','padding','exact input-derived samples and padding'),
      ('native-order-maxtwo','order','Complete channel-major physical allocation order'),
      ('native-range-offset','range','exact native source/range'),
      ('native-range-offset-float','range-offset-float','native byte reference field types'),
      ('native-range-length-float','range-length-float','native byte reference field types'),
      ('sidecar-source-URL','template','Complete single-source reader URL templates'),
      ('sidecar-codec','codec','Native Zarr descriptor: exact JSON types/values'),
      ('sidecar-inline-value','inline','exact inline numeric/byte payload'),
      ('sidecar-root-attributes','side-attrs','.zattrs: exact JSON types/values'),
      ('sidecar-extra-key','side-key','Complete sidecar reference/key inventory'),
      ('sidecar-version-bool','version-bool','Complete sidecar root inventory/version'),
      ('sidecar-version-float','version-float','Complete sidecar root inventory/version'),
      ('cached-schema-doc','schema','cache payload: exact JSON types/values'),
      ('companion-metadata-value','companion-metadata','settings/sampling: complete payload'),
      ('companion-independent-alias','companion-alias','exact source alias identity'),
      ('companion-channel-selection','companion-channel','VDS dataspace extents'),
      ('companion-sibling-target','companion-sibling','Exact VDS sibling filename/dataset'),
      ('companion-extra-attribute','companion-attr','complete companion attributes'),
      ('effective-companion-option-omitted','option-missing','Explicit effective companion option'),
      ('disabled-companion-requested','option-enabled','Complete requested sibling inventory'),
      ('publication-wrong-size','size','Actual publication size/SHA'),
      ('publication-wrong-sha','sha','Actual publication size/SHA'),
      ('publication-wrong-artifact-URI','uri','Exact publication artifact URI/name'),
      ('publication-retargeted-reader','reader','Reviewed publication reader URI'),
      ('publication-version-bool','receipt-version','Actual test-local publication receipt'),
      ('final-metadata-command-input','final-command','Final NWB command binding: validate_metadata'),
      ('final-metadata-before-SHA','final-sha','Exact final NWB before/after validate_metadata'),
      ('inspector-finding-omitted','inspector-drop','Complete inspector finding multiset'),
      ('inspector-finding-duplicated','inspector-duplicate','Complete inspector finding multiset'),
      ('culture-recommendation-omitted','recommendation','Complete recommendation multiset'),
      ('report-integer-version-bool','report-version','Complete remaining validation report fields/types'),
      ('disabled-validator-result','validator-result','Complete actual optional-companion validation result'),
      ('wrong-selected-raw-path','selected','Invocation selected raw path'),
    ]
    tests=[dict(id=name,kind=kind,expected_error=error,case='maxtwo_routed',mode='disabled' if kind in ['option-enabled','validator-result'] else 'enabled') for name,kind,error in rows]
    for kind in ['padding','order']:
        old=next(x for x in tests if x['kind']==kind)
        tests.append(dict(old,id='native-'+kind+'-maxone',case='maxone'))
    return tests


def attributes(obj):
    result=[]
    for key in obj.attrs:
        attr=obj.attrs.get_id(key)
        try:result.append((key,obj.attrs[key],attr.dtype))
        finally:attr.close()
    return result


def restore_attributes(obj,rows):
    for key,value,dtype in rows:obj.attrs.create(key,value,dtype=dtype)


def native_ranges(dataset):
    rows=[]
    dataset.id.chunk_iter(lambda x:rows.append(dict(coordinate=list(x.chunk_offset),offset=x.byte_offset,bytes=x.size,mask=x.filter_mask)))
    return sorted(rows,key=lambda x:x['coordinate'])


def resync_ranges(nwb,sidecar):
    with h5py.File(nwb,'r') as f:
        d=f[SIGNAL];rows=native_ranges(d)
        for row in rows:
            key=SIDE_SIGNAL+'/'+str(row['coordinate'][0]//d.chunks[0])+'.'+str(row['coordinate'][1]//d.chunks[1])
            require(key in sidecar['refs'],'Existing native reference coordinate')
            sidecar['refs'][key][1:]=[row['offset'],row['bytes']]
    return rows


def mutate(tree,test,support):
    case=test['case'];kind=test['kind'];folder=tree/'artifacts'/case
    nwb=folder/'output.nwb';sidepath=folder/'output.nwb.lindi.json';reportpath=folder/'output.nwb.validation.json'
    receiptpath=tree/'receipts'/(case+'.json');receipt=read(receiptpath);side=read(sidepath);report=read(reportpath)
    facts=read(tree/'inputs'/case/'facts.json');selected,=[x for x in facts['stores'] if x['selected']]
    evidence={};write_side=False;write_report=False;changed_nwb=False
    if kind in ['chunks','filter','mask','padding','order','schema']:
        with h5py.File(nwb,'r+') as f:
            d=f[SIGNAL];before=native_ranges(d)
            if kind in ['chunks','filter']:
                values=d[:];attrs=attributes(d);old_chunks=d.chunks;del f[SIGNAL]
                d=f.create_dataset(SIGNAL,data=values,dtype=values.dtype,chunks=(100,1) if kind=='chunks' else old_chunks,compression=401 if kind=='chunks' else 'gzip')
                restore_attributes(d,attrs)
                evidence=dict(before=before,after_chunks=list(d.chunks),after_pipeline=storage.pipeline(d))
            elif kind=='mask':
                payload=d[:d.chunks[0],:d.chunks[1]].tobytes();old_mask,old_raw=d.id.read_direct_chunk((0,0))
                d.id.write_direct_chunk((0,0),payload,filter_mask=1);f.flush()
                evidence=dict(coordinate=[0,0],before_mask=old_mask,after_mask=d.id.read_direct_chunk((0,0))[0],before_encoded_sha256=hashlib.sha256(old_raw).hexdigest(),after_encoded_sha256=hashlib.sha256(payload).hexdigest())
            elif kind=='padding':
                from maxwell_lindi_codec import MaxwellMxwDataCodec
                coordinate=((d.shape[0]//d.chunks[0])*d.chunks[0],0);mask,raw=d.id.read_direct_chunk(coordinate)
                decoded=np.frombuffer(MaxwellMxwDataCodec().decode(raw),dtype='<u2').copy().reshape(d.chunks)
                valid=d.shape[0]-coordinate[0];require(0<valid<d.chunks[0] and decoded[valid,0]==0,'Real zero partial-chunk padding')
                decoded[valid,0]=1;support.mkdir();encoding=support/'encoded-padding.h5'
                with h5py.File(encoding,'w') as temporary:
                    encoded=temporary.create_dataset('raw',data=decoded,dtype='<u2',chunks=d.chunks,compression=401)
                    new_mask,new_raw=encoded.id.read_direct_chunk((0,0));require(new_mask==0,'Actual native encoding must apply filter401')
                d.id.write_direct_chunk(coordinate,new_raw,filter_mask=0);f.flush()
                evidence=dict(coordinate=list(coordinate),first_padding_row=valid,padding_before=0,padding_after=1,before_encoded_sha256=hashlib.sha256(raw).hexdigest(),after_encoded_sha256=hashlib.sha256(new_raw).hexdigest(),source_filter_mask=mask,encoding_file=str(encoding))
            elif kind=='order':
                require(f.id.get_create_plist().get_file_space_strategy()[0]==3,'Pinned allocator must disable free-space reuse')
                mask,raw=d.id.read_direct_chunk((0,0));original_offset=before[0]['offset'];last=max(x['offset'] for x in before)
                d.id.write_direct_chunk((0,0),b'\0'*(len(raw)+4096),filter_mask=0)
                d.id.write_direct_chunk((0,0),raw,filter_mask=mask);f.flush();after=native_ranges(d)
                moved=next(x for x in after if x['coordinate']==[0,0]);require(moved['offset']>last,'Real physical first-chunk relocation')
                require(d.id.read_direct_chunk((0,0))==(mask,raw),'Encoded chunk bytes and mask remain exact')
                evidence=dict(coordinate=[0,0],before_offset=original_offset,after_offset=moved['offset'],original_last_chunk_offset=last,encoded_sha256=hashlib.sha256(raw).hexdigest(),encoded_bytes_unchanged=True)
            else:
                path='/specifications/core/2.10.0/namespace';old=f[path][()];value=json.loads(old)
                before_doc=value['namespaces'][0]['doc'];value['namespaces'][0]['doc']=before_doc+' (counterexample)'
                controls.replace_dataset(f,path,json.dumps(value,separators=(',',':')))
                evidence=dict(path=path,before_doc=before_doc,after_doc=value['namespaces'][0]['doc'],schema_version_unchanged=True)
        changed_nwb=True
        if kind in ['mask','padding','order','filter']:
            evidence['actual_native_ranges_after']=resync_ranges(nwb,side);write_side=True
    elif kind in ['range','range-offset-float','range-length-float','template','codec','inline','side-attrs','side-key','version-bool','version-float']:
        refs=side['refs'];key=SIDE_SIGNAL+'/0.0'
        if kind=='range':before=refs[key][1];refs[key][1]+=1;evidence=dict(key=key,before_offset=before,after_offset=refs[key][1])
        elif kind in ['range-offset-float','range-length-float']:
            index=1 if kind=='range-offset-float' else 2;before=typed(refs[key][index]);refs[key][index]=float(refs[key][index]);evidence=dict(key=key,index=index,before=before,after=typed(refs[key][index]))
        elif kind=='template':before=side['templates']['u1'];side['templates']['u1']='file:///counterexample/wrong.nwb';evidence=dict(before=before,after=side['templates']['u1'])
        elif kind=='codec':descriptor=refs[SIDE_SIGNAL+'/.zarray'];before=dict(descriptor['filters'][0]);descriptor['filters'][0]['id']='unrecognized-counterexample-codec';evidence=dict(before=before,after=descriptor['filters'][0])
        elif kind=='inline':
            path='general/extracellular_ephys/electrodes/rel_x';descriptor=refs[path+'/.zarray'];require(descriptor['filters'] is None,'Reviewed numeric inline filter contract')
            codec=numcodecs.get_codec(descriptor['compressor']);raw=codec.decode(base64.b64decode(refs[path+'/0'][7:],validate=True));values=np.frombuffer(raw,dtype=descriptor['dtype']).copy();before=values[0].item();values[0]+=1.0
            refs[path+'/0']='base64:'+base64.b64encode(codec.encode(values.tobytes())).decode();evidence=dict(path=path,before=typed(before),after=typed(values[0].item()))
        elif kind=='side-attrs':before=refs['.zattrs']['nwb_version'];refs['.zattrs']['nwb_version']='2.9.0';evidence=dict(before=before,after='2.9.0')
        elif kind=='side-key':refs['unexpected/.zgroup']={'zarr_format':2};evidence=dict(extra_key='unexpected/.zgroup')
        else:before=typed(side['version']);side['version']=True if kind=='version-bool' else 1.0;evidence=dict(before=before,after=typed(side['version']))
        write_side=True
    elif kind.startswith('companion-'):
        path=folder/'output.linked.raw.h5'
        with h5py.File(path,'r+') as f:
            if kind=='companion-metadata':
                d=f[selected['store']+'/settings/sampling'];before=d[:].tolist();d[0]+=1;evidence=dict(path=d.name,before=before,after=d[:].tolist())
            elif kind=='companion-alias':
                path='/wells/'+selected['well']+'/rec0000';source=selected['store'];require(f[path].id==f[source].id,'Original selected store alias')
                del f[path];f.copy(source,path);require(f[path].id!=f[source].id,'Actual independent physical alias copy')
                evidence=dict(path=path,source=source,before_shared_storage=True,after_shared_storage=False)
            elif kind=='companion-attr':f.attrs['unexpected_counterexample']=np.int64(1);evidence=dict(added_attribute='unexpected_counterexample')
            else:
                path=selected['raw_dataset_path'];d=f[path];shape=d.shape;attrs=attributes(d);before_head=d[:,:8].tolist();dtype=d.dtype;fill=d.fillvalue;del f[path]
                layout=h5py.VirtualLayout(shape=shape,dtype=dtype);filename='other.nwb' if kind=='companion-sibling' else 'output.nwb'
                target=h5py.VirtualSource(filename,SIGNAL,shape=(shape[1],shape[0]))
                for channel in range(shape[0]):layout[channel,:]=target[:,(channel+1)%shape[0] if kind=='companion-channel' else channel]
                d=f.create_virtual_dataset(path,layout,fillvalue=fill);restore_attributes(d,attrs);after_head=d[:,:8].tolist();require(before_head!=after_head,'Real changed companion samples')
                evidence=dict(path=path,before_head=before_head,after_head=after_head,source_filename=filename,source_channels=[(c+1)%shape[0] if kind=='companion-channel' else c for c in range(shape[0])])
    elif kind in ['inspector-drop','inspector-duplicate','recommendation','report-version']:
        if kind=='inspector-drop':evidence=dict(removed_issue=report['nwb_inspector']['issues'].pop())
        elif kind=='inspector-duplicate':report['nwb_inspector']['issues'].append(dict(report['nwb_inspector']['issues'][0]));evidence=dict(duplicated_issue=report['nwb_inspector']['issues'][0])
        elif kind=='recommendation':evidence=dict(removed_recommendation=report['ndx_cell_culture_recommendations'].pop())
        else:evidence=dict(before=typed(report['report_schema_version']),after=typed(True));report['report_schema_version']=True
        write_report=True
    elif kind=='reader':
        before=receipt['reader_url'];new='file:///unreviewed/'+case+'/output.nwb';receipt['reader_url']=new;side['templates']['u1']=new;side['generationMetadata']['sourceUrl']=new
        phase=next(x for x in receipt['phases'] if x['phase']=='lindi');phase['command'][phase['command'].index('--source-url')+1]=new
        for item in receipt['artifacts']:item['uri']=new.rsplit('/',1)[0]+'/'+item['name']
        write_side=True;evidence=dict(before=before,after=new,coherent_sidecar_and_receipt_retarget=True)
    elif kind=='option-missing':evidence=dict(removed_option=receipt['settings'].pop('create_linked_maxwell_companion'))
    elif kind=='option-enabled':receipt['settings']['create_linked_maxwell_companion']=True;evidence=dict(before=False,after=True,actual_companion_still_omitted=True)
    elif kind=='receipt-version':receipt['schema_version']=True;evidence=dict(before=typed(1),after=typed(True))
    elif kind=='uri':before=receipt['artifacts'][0]['uri'];receipt['artifacts'][0]['uri']=before+'.wrong';evidence=dict(before=before,after=receipt['artifacts'][0]['uri'])
    elif kind in ['size','sha']:
        item=receipt['artifacts'][0];before=item['size' if kind=='size' else 'sha256'];item['size' if kind=='size' else 'sha256']=before+1 if kind=='size' else '0'*64;evidence=dict(before=before,after=item['size' if kind=='size' else 'sha256'])
    elif kind in ['final-command','final-sha']:
        phase=next(x for x in receipt['phases'] if x['phase']=='validate_metadata')
        if kind=='final-command':index=phase['command'].index('--nwb-file')+1;before=phase['command'][index];phase['command'][index]=str(Path(before).with_name('converted.nwb'));evidence=dict(before=before,after=phase['command'][index])
        else:before=phase['nwb_sha256_before'];phase['nwb_sha256_before']=next(x for x in receipt['phases'] if x['phase']=='convert')['nwb_sha256_after'];evidence=dict(before=before,after=phase['nwb_sha256_before'])
    elif kind=='validator-result':
        actual=receipt['runtime_validation'];value=json.loads(base64.b64decode(actual['result_bytes_b64']));require(value['all_linked_values_equal'] is None,'Actual omitted companion result')
        value['all_linked_values_equal']=False;raw=json.dumps(value).encode();actual['result_bytes_b64']=base64.b64encode(raw).decode();actual['result_sha256']=hashlib.sha256(raw).hexdigest();evidence=dict(before=None,after=False)
    elif kind=='selected':before=receipt['selected_raw_dataset'];receipt['selected_raw_dataset']=before.replace('data0001','data0000');evidence=dict(before=before,after=receipt['selected_raw_dataset'])
    else:raise ValueError('Unsupported physical control '+kind)
    if write_side:sidepath.write_text(json.dumps(side,indent=2)+'\n')
    if write_report:reportpath.write_text(json.dumps(report,indent=2)+'\n')
    resealed=[]
    if kind not in ['size','sha']:
        for item in receipt['artifacts']:
            path=folder/item['name'];old=dict(item);item['size']=path.stat().st_size;item['sha256']=sha(path)
            if item!=old:resealed.append(dict(before=old,after=dict(item)))
    if changed_nwb:
        digest=sha(nwb)
        for phase in receipt['phases']:
            if phase['phase']=='repack':phase['nwb_sha256_after']=digest
            elif phase['phase'] not in ['validate_input','convert']:phase['nwb_sha256_before']=phase['nwb_sha256_after']=digest
    receiptpath.write_text(json.dumps(receipt,indent=2)+'\n')
    return dict(change=evidence,synthetic_counterexample_receipt=True,resealed_artifact_identities=resealed,synthetic_final_NWB_phase_hashes=changed_nwb)


def setup_plugin():
    sys.path.insert(0,'/opt/ephys-modular/maxwell/scripts')
    from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
    configure_bundled_maxwell_hdf5_plugin()


def prepare(inputs,positives,reference,output):
    setup_plugin();require(not output.exists(),'New physical control output required');output.mkdir()
    for name in ['baselines','cases','restored','support']:(output/name).mkdir()
    original=dict(inputs=inventory(inputs),positives=inventory(positives),reference=sha(reference));baselines={};records=[]
    for mode in ['enabled','disabled']:
        tree=output/'baselines'/mode;tree.mkdir();shutil.copytree(inputs,tree/'inputs');shutil.copytree(positives/mode/'artifacts',tree/'artifacts');shutil.copytree(positives/mode/'receipts',tree/'receipts');shutil.copy2(reference,tree/'schema-reference.nwb');baselines[mode]=inventory(tree)
    for test in definitions():
        baseline=output/'baselines'/test['mode'];work=output/'restored'/test['id'];shutil.copytree(baseline,work)
        evidence=mutate(work,test,output/'support'/test['id']);changed=inventory(work);source=baselines[test['mode']]
        require(set(changed)==set(source),'Physical controls preserve fixture file inventory')
        altered=[k for k in changed if changed[k]!=source[k]];require(altered,'Real byte changes required');require(not any(k.startswith('inputs/') for k in altered),'All input truth stays unchanged')
        shutil.copytree(work,output/'cases'/test['id'])
        for key in altered:shutil.copy2(baseline/key,work/key)
        require(inventory(work)==source,'Complete exact byte restoration')
        records.append(dict(test,mutation=evidence,changed_files={k:dict(original=source[k],mutated=changed[k],restored=source[k]) for k in altered},mutated_inventory=changed,restored_inventory=inventory(work)))
        print(json.dumps(dict(prepared=test['id'],changed=altered)),flush=True)
    require(original==dict(inputs=inventory(inputs),positives=inventory(positives),reference=sha(reference)),'Original frozen producer/input bytes stay exact')
    write_new(output/'controls.json',dict(schema_version=1,scope='physical_artifact_and_synthetic_receipt_controls',source_inputs=original,baselines=baselines,controls=records,all_counterexample_receipts_synthetic=True,producer_receipts_relabelled=False))
    return dict(passed=True,prepared=len(records))


def one(root,identifier,report):
    controls.readonly_mounts();setup_plugin();manifest=read(root/'controls.json');test,=[x for x in manifest['controls'] if x['id']==identifier]
    tree=root/'cases'/identifier;baseline=root/'baselines'/test['mode'];restored=root/'restored'/identifier;start=time.monotonic()
    require(inventory(tree)==test['mutated_inventory'] and inventory(restored)==test['restored_inventory']==manifest['baselines'][test['mode']],'Exact counterexample/restoration identities')
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter('always');schema_errors=[str(x) for x in validate(path=str(tree/'artifacts'/test['case']/'output.nwb'))]
        # Original receipts establish independent unchanged science even when the
        # negative intentionally corrupts its copied selection or invocation receipt.
        science=canonical.verify(tree/'inputs',tree/'artifacts',baseline/'receipts')
        failure=None
        try:artifacts.verify(tree/'inputs',tree/'artifacts',tree/'receipts',tree/'schema-reference.nwb')
        except Exception as exc:failure=dict(type=type(exc).__name__,message=str(exc),traceback=traceback.format_exc(),frames=[dict(file=Path(x.filename).name,function=x.name,line=x.lineno) for x in traceback.extract_tb(exc.__traceback__)])
    expected=bool(failure and failure['type']=='ValueError' and test['expected_error'] in failure['message'] and failure['frames'][-1]['file'] in ['canonical.py','storage.py','artifacts.py'])
    result=dict(id=identifier,kind=test['kind'],case=test['case'],mode=test['mode'],passed=expected and not schema_errors,expected_error=test['expected_error'],failure=failure,schema_valid=not schema_errors,schema_errors=schema_errors,unchanged_independent_science_passed=science['passed'],scientific_cases_checked=len(science['cases']),mutation=test['mutation'],changed_files=test['changed_files'],restored_byte_identical=True,original_facts_unchanged=True,duration_s=time.monotonic()-start,peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,warnings=[str(x.message) for x in captured])
    write_new(report,result);return result


def check(root,reports):
    require(sys.version_info[:2]==(3,11),'Pinned Python3.11');mounts=controls.readonly_mounts();setup_plugin();manifest=read(root/'controls.json');before=inventory(root);positive=[]
    for mode in ['enabled','disabled']:
        tree=root/'baselines'/mode;require(inventory(tree)==manifest['baselines'][mode],'Unchanged original positive bytes')
        result=artifacts.verify(tree/'inputs',tree/'artifacts',tree/'receipts',tree/'schema-reference.nwb');require(result['passed'],'Unchanged complete positive');write_new(reports/('positive-'+mode+'.json'),result);positive.append(dict(mode=mode,cases=len(result['cases']),passed=True))
    def run(test):
        dest=reports/(test['id']+'.json');cmd=[sys.executable,str(Path(__file__).resolve()),'one','--controls',str(root),'--id',test['id'],'--report',str(dest)];start=time.monotonic()
        with (reports/(test['id']+'-stdout.log')).open('x') as stream:
            try:actual=subprocess.run(cmd,stdout=stream,stderr=subprocess.STDOUT,timeout=90).returncode
            except subprocess.TimeoutExpired:actual='timeout90s'
        if dest.exists():
            result=read(dest);require((actual==0)==result['passed'],'Actual worker exit agrees with its result')
        else:result=dict(id=test['id'],kind=test['kind'],case=test['case'],mode=test['mode'],passed=False,schema_valid=False,failure=dict(type='WorkerFailure',message=str(actual)),duration_s=time.monotonic()-start);write_new(dest,result)
        result['actual_worker_command']=cmd;result['actual_worker_exit']=actual;write_new(reports/(test['id']+'-worker.json'),dict(command=cmd,actual_exit=actual,duration_s=time.monotonic()-start));return result
    results=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        for item in pool.map(run,manifest['controls']):results.append(item);print(json.dumps({k:item[k] for k in ['id','passed','schema_valid','duration_s']}|dict(error=None if item.get('failure') is None else item['failure']['message'])),flush=True)
    require(inventory(root)==before,'Read-only control/root/fact/receipt bytes remain exact')
    import runtime_identity
    result=dict(passed=all(x['passed'] for x in results),scope=manifest['scope'],total_controls=len(results),controls=results,positive=positive,read_only_mounts=mounts,actual_uid=os.getuid(),actual_gid=os.getgid(),runtime_identity=runtime_identity.identity(),all_original_mutated_and_restored_bytes_unchanged_during_check=True,all_counterexample_receipts_synthetic=True,producer_graph_execution_claimed=False,generic_LINDI_compatibility_claimed=False,full_goal_complete=False)
    write_new(reports/'controls.json',result);return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);subs=p.add_subparsers(dest='action',required=True)
    x=subs.add_parser('prepare')
    for name in ['inputs','positives','reference','output']:x.add_argument('--'+name,required=True,type=Path)
    x=subs.add_parser('check')
    for name in ['controls','reports']:x.add_argument('--'+name,required=True,type=Path)
    x=subs.add_parser('one')
    x.add_argument('--controls',required=True,type=Path);x.add_argument('--id',required=True);x.add_argument('--report',required=True,type=Path)
    a=p.parse_args()
    result=prepare(a.inputs,a.positives,a.reference,a.output) if a.action=='prepare' else check(a.controls,a.reports) if a.action=='check' else one(a.controls,a.id,a.report)
    print(json.dumps({k:v for k,v in result.items() if k not in ['controls','runtime_identity']},indent=2))
    if not result['passed']:raise SystemExit(1)
