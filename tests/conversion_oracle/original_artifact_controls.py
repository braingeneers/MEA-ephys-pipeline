#!/usr/bin/env python3
"""Real semantic controls for original companions/sidecars/reports/publication.

Candidate receipts and files are synthetic test inputs. Original producer evidence
is never changed or resealed. Semantic checks deliberately precede publication
identity checks where needed, so a stale whole-file checksum is not the evidence.
"""
from pathlib import Path
import argparse
import base64
import hashlib
import json
import resource
import shutil
import subprocess
import sys
import time
import traceback

import h5py
import numpy as np
import zarr

import artifacts
import canonical
import original_artifacts as oracle
from original_nwb import FrozenOriginalScope
from inputs import sha


def scope_at(original, proof):
    return FrozenOriginalScope(original,sha(original/'freeze.json'),proof,sha(proof/'freeze.json'))


def inline_change(sidecar, path):
    store={}
    for key,value in sidecar['refs'].items():
        if isinstance(value,dict): store[key]=json.dumps(value).encode()
        elif isinstance(value,str): store[key]=base64.b64decode(value[7:]) if value.startswith('base64:') else value.encode('ascii')
    group=zarr.open_group(store=store,mode='a')
    if path=='schema':
        path=next(key.removesuffix('/.zarray') for key in sidecar['refs'] if key.startswith('specifications/') and key.endswith('/.zarray'))
        array=group[path];old=array[0]
        # Scalar schema strings have a fixed-width Unicode encoding. Appending
        # silently truncates, so change one stored character at the same length.
        text=str(old);position=next(i for i,char in enumerate(text) if char.isalpha())
        changed=text[:position]+('x' if text[position]!='x' else 'y')+text[position+1:]
        json.loads(changed)
        array[0]=changed
        assert str(array[0])==changed and str(array[0])!=text
    else:
        array=group[path];array[0]=np.nextafter(array[0],np.inf)
    key=path+'/0'
    old_encoded=sidecar['refs'][key]
    old_bytes=base64.b64decode(old_encoded[7:]) if old_encoded.startswith('base64:') else old_encoded.encode('ascii')
    new_bytes=bytes(store[key])
    assert old_bytes!=new_bytes, 'Control must change the inline payload bytes'
    sidecar['refs'][key]='base64:'+base64.b64encode(new_bytes).decode()
    return dict(path=path,key=key,before_sha256=hashlib.sha256(old_bytes).hexdigest(),
                after_sha256=hashlib.sha256(new_bytes).hexdigest(),stored_payload_really_changed=True)


def prepare(original,proof,output):
    output.mkdir(parents=True,exist_ok=False)
    scope=scope_at(original,proof)
    folder,facts,receipt,nwb,binding=scope.bind('enabled','maxone')
    nodes,_,_=canonical.expected_graph(folder,facts,receipt)
    baseline=output/'baseline';baseline.mkdir()
    shutil.copytree(nwb.parent,baseline/'artifacts')
    (baseline/'receipt.json').write_bytes((original/'receipts/enabled/maxone.json').read_bytes())
    definitions=[
        ('companion-raw-copy','companion','exact retained original raw chunk bytes/mask'),
        ('companion-metadata','companion','complete original raw-source metadata'),
        ('companion-bridge','companion','actual original bridge marker'),
        ('companion-root-marker','companion','complete original companion attributes'),
        ('companion-missing-mapping','companion','complete original companion children'),
        ('companion-vds-file','companion','Complete original VDS mapping target'),
        ('companion-vds-channel','companion','Complete original VDS extents'),
        ('sidecar-version-bool','sidecar','Complete sidecar root inventory/version'),
        ('sidecar-version-float','sidecar','Complete sidecar root inventory/version'),
        ('sidecar-template','sidecar','Complete single-source reader URL templates'),
        ('sidecar-generation','sidecar','Complete LINDI generation metadata'),
        ('sidecar-extra-key','sidecar','Complete sidecar reference/key inventory'),
        ('sidecar-missing-alias','sidecar','missing metadata'),
        ('sidecar-alias-uuid','sidecar','exact JSON types/values'),
        ('sidecar-parent-age','sidecar','exact JSON types/values'),
        ('sidecar-native-offset-float','sidecar','native byte reference field types'),
        ('sidecar-native-length-bool','sidecar','native byte reference field types'),
        ('sidecar-native-url','sidecar','exact native source/range'),
        ('sidecar-inline-numeric','sidecar','exact inline numeric/byte payload'),
        ('sidecar-inline-schema','sidecar','exact inline/reference payload'),
        ('report-missing-finding','report','Complete inspector finding multiset'),
        ('report-duplicate-finding','report','Complete inspector finding multiset'),
        ('report-recommendation','report','Complete recommendation multiset'),
        ('report-extra-field','report','Complete validation report root inventory'),
        ('report-version-bool','report','Complete remaining validation report fields/types'),
        ('publication-sha','publication','Actual publication size/SHA'),
        ('publication-size-float','publication','Actual publication size/SHA'),
        ('publication-uri','publication','Exact publication artifact URI/name'),
        ('publication-missing-sibling','publication','Complete publication receipt inventory'),
        ('publication-option','publication','Complete requested sibling inventory'),
        ('invocation-order','invocations','Complete original scientific phase order'),
        ('invocation-final-report','invocations','Exact original scientific CLI/options: validate_metadata'),
        ('invocation-final-sha','invocations','Original final NWB before/after validate_metadata'),
        ('invocation-native-tool','invocations','Exact original scientific CLI/options: convert'),
    ]
    rows=[]
    for name,kind,expected in definitions:
        case=output/name;shutil.copytree(baseline,case)
        published=case/'artifacts'
        candidate=json.loads((case/'receipt.json').read_text())
        sidepath=published/'output.nwb.lindi.json'
        reportpath=published/'output.nwb.validation.json'
        side=json.loads(sidepath.read_text());report=json.loads(reportpath.read_text())
        mutation={}
        if kind=='companion':
            with h5py.File(published/'output.linked.raw.h5','r+') as h:
                raw=receipt['selected_raw_dataset']
                if name=='companion-raw-copy':
                    data=h['/recordings/rec0000/well000/groups/routed/raw'];data[0,0]=int(data[0,0])+1
                elif name=='companion-metadata':
                    data=h['/recordings/rec0000/well000/settings/lsb'];data[0]=np.nextafter(data[0],np.inf)
                elif name=='companion-bridge':h[raw].attrs.modify('legacy_data_bridge_target_file','wrong.nwb')
                elif name=='companion-root-marker':h.attrs['braingeneers_linked_companion_scope']='selected_unit'
                elif name=='companion-missing-mapping':del h['/data_store/data0000/settings/mapping']
                else:
                    old=h[raw];shape=old.shape;dtype=old.dtype;attrs=dict(old.attrs)
                    del h[raw]
                    source=h5py.VirtualSource('wrong.nwb' if name=='companion-vds-file' else 'output.nwb',oracle.SIGNAL,shape=(shape[1],shape[0]))
                    layout=h5py.VirtualLayout(shape=shape,dtype=dtype)
                    for channel in range(shape[0]):layout[channel,:]=source[:,channel if name=='companion-vds-file' else shape[0]-1-channel]
                    target=h.create_virtual_dataset(raw,layout)
                    for key,value in attrs.items():target.attrs[key]=value
        elif kind=='sidecar':
            key='acquisition/ElectricalSeries/data/0.0'
            if name=='sidecar-version-bool':side['version']=True
            elif name=='sidecar-version-float':side['version']=1.0
            elif name=='sidecar-template':side['templates']['u1']='file:///wrong/output.nwb'
            elif name=='sidecar-generation':side['generationMetadata']['sourceNwbFile']='converted.nwb'
            elif name=='sidecar-extra-key':side['refs']['unreviewed/.zattrs']={'extra':True}
            elif name=='sidecar-missing-alias':del side['refs']['~obj_pointed_by_10328/.zattrs']
            elif name=='sidecar-alias-uuid':side['refs']['~obj_pointed_by_10328/.zattrs']['object_id']='00000000-0000-4000-8000-000000000000'
            elif name=='sidecar-parent-age':side['refs'][nodes['cultures/parent-a'].path.lstrip('/')+'/.zattrs']['age']='P75D'
            elif name=='sidecar-native-offset-float':side['refs'][key][1]=float(side['refs'][key][1])
            elif name=='sidecar-native-length-bool':side['refs'][key][2]=True
            elif name=='sidecar-native-url':side['refs'][key][0]='file:///wrong/output.nwb'
            elif name=='sidecar-inline-numeric':mutation=inline_change(side,'general/extracellular_ephys/electrodes/rel_x')
            else:mutation=inline_change(side,'schema')
            sidepath.write_text(json.dumps(side,indent=2)+'\n')
        elif kind=='report':
            if name=='report-missing-finding':report['nwb_inspector']['issues'].pop()
            elif name=='report-duplicate-finding':report['nwb_inspector']['issues'].append(dict(report['nwb_inspector']['issues'][0]))
            elif name=='report-recommendation':report['ndx_cell_culture_recommendations'].pop()
            elif name=='report-extra-field':report['unreviewed']=True
            else:report['report_schema_version']=True
            reportpath.write_text(json.dumps(report,indent=2)+'\n')
        elif kind=='publication':
            if name=='publication-sha':candidate['artifacts'][0]['sha256']='0'*64
            elif name=='publication-size-float':candidate['artifacts'][0]['size']=float(candidate['artifacts'][0]['size'])
            elif name=='publication-uri':candidate['artifacts'][0]['uri']='file:///wrong/output.nwb'
            elif name=='publication-missing-sibling':candidate['artifacts'].pop()
            else:candidate['settings']['create_linked_maxwell_companion']=False
        else:
            by_name={p['phase']:p for p in candidate['phases']}
            if name=='invocation-order':candidate['phases'].append(candidate['phases'].pop(3))
            elif name=='invocation-final-report':
                command=by_name['validate_metadata']['command'];command[command.index('--nwb-file')+1]=command[command.index('--nwb-file')+1].replace('output.nwb','converted.nwb')
            elif name=='invocation-final-sha':by_name['validate_metadata']['nwb_sha256_before']='0'*64
            else:by_name['convert']['command'][1]='/opt/ephys-modular/maxwell/scripts/convert_maxone_to_nwb.py'
        if kind in ('publication','invocations'):(case/'receipt.json').write_text(json.dumps(candidate,indent=2)+'\n')
        changed={}
        for path in case.rglob('*'):
            if path.is_file() and sha(path)!=sha(baseline/path.relative_to(case)):changed[str(path.relative_to(case))]=sha(path)
        assert changed and sha(published/'output.nwb')==sha(nwb),name
        rows.append(dict(name=name,kind=kind,expected_error=expected,changed_files=changed,mutation=mutation,
                         semantic_check_before_publication_checksum=True,synthetic_candidate_not_producer_evidence=True))
    manifest=dict(controls=rows,original_freeze_sha256=sha(original/'freeze.json'),proof_freeze_sha256=sha(proof/'freeze.json'),
                  source_nwb_sha256=sha(nwb),original_producer_evidence_modified=False)
    (output/'controls.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(dict(prepared=len(rows),all_real_changed_files=True)))


def one(controls,index,report,original,proof):
    manifest=json.loads((controls/'controls.json').read_text());row=manifest['controls'][index]
    scope=scope_at(original,proof)
    folder,facts,receipt,nwb,binding=scope.bind('enabled','maxone')
    candidate=controls/row['name'];published=candidate/'artifacts'
    assert sha(published/'output.nwb')==manifest['source_nwb_sha256']==sha(nwb)
    for name,digest in row['changed_files'].items():assert sha(candidate/name)==digest
    proposal=json.loads((candidate/'receipt.json').read_text())
    started=time.monotonic()
    try:
        if row['kind']=='companion':oracle.companion(oracle.actual_source(scope,'maxone',receipt),published/'output.linked.raw.h5',published/'output.nwb',folder,facts,receipt)
        elif row['kind']=='sidecar':oracle.sidecar(nwb,json.loads((published/'output.nwb.lindi.json').read_text()),receipt,folder,facts)
        elif row['kind']=='report':artifacts.report_contents(json.loads((published/'output.nwb.validation.json').read_text()),
            artifacts.expected_report(folder,facts,receipt,'/evidence/runs/enabled/maxone/output.nwb'))
        elif row['kind']=='publication':artifacts.publication(folder,published,proposal,'file:///evidence/published/enabled/')
        else:oracle.invocations(proposal,'maxone','enabled',sha(nwb))
    except ValueError as error:
        assert row['expected_error'] in str(error),(row['name'],row['expected_error'],str(error))
        result=dict(name=row['name'],kind=row['kind'],passed=True,exception_type=type(error).__name__,intended_error=str(error),
                    frames=traceback.format_exc(),changed_files=row['changed_files'],source_nwb_unchanged=True,
                    original_producer_receipts_modified=False,semantic_check_before_publication_checksum=True,
                    duration_s=time.monotonic()-started,peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    else:raise AssertionError('Changed original artifact accepted: '+row['name'])
    with report.open('x') as stream:stream.write(json.dumps(result,indent=2)+'\n')


def check(controls,reports,original,proof,reference):
    reports.mkdir(parents=True,exist_ok=False)
    manifest=json.loads((controls/'controls.json').read_text())
    positive=oracle.verify(scope_at(original,proof),reference)
    assert positive['completed'] and positive['all_original_scientific_values_and_artifact_bindings_match'] and not positive['all_native_invariants_satisfied']
    (reports/'positive.json').write_text(json.dumps(positive,indent=2)+'\n')
    rows=[]
    for i,row in enumerate(manifest['controls']):
        dest=reports/(row['name']+'.json')
        command=[sys.executable,__file__,'one','--controls',str(controls),'--index',str(i),'--report',str(dest),'--original',str(original),'--proof',str(proof)]
        (reports/(row['name']+'-command.json')).write_text(json.dumps(command,indent=2)+'\n')
        with (reports/(row['name']+'.log')).open('w') as stream:process=subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,timeout=90)
        (reports/(row['name']+'-exit.json')).write_text(json.dumps(dict(exit=process.returncode))+'\n')
        assert process.returncode==0,(row['name'],process.returncode)
        rows.append(json.loads(dest.read_text()));print(row['name'],'intended semantic rejection',flush=True)
    assert sha(original/'freeze.json')==manifest['original_freeze_sha256'] and sha(proof/'freeze.json')==manifest['proof_freeze_sha256']
    result=dict(passed=True,real_semantic_controls=len(rows),controls=rows,all_four_unchanged_complete_positive_cases=True,
                original_producer_evidence_modified=False,stale_publication_checksums_not_used_as_semantic_rejection=True,
                no_timeout_oom_or_unrelated_error_accepted=True,all_native_original_invariants_satisfied=False)
    with (reports/'summary.json').open('x') as stream:stream.write(json.dumps(result,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='operation',required=True)
    prep=sub.add_parser('prepare');run=sub.add_parser('check');worker=sub.add_parser('one')
    for part in (prep,run,worker):
        part.add_argument('--original',required=True,type=Path);part.add_argument('--proof',required=True,type=Path)
    prep.add_argument('--output',required=True,type=Path)
    for part in (run,worker):part.add_argument('--controls',required=True,type=Path)
    run.add_argument('--reports',required=True,type=Path);run.add_argument('--reference',required=True,type=Path)
    worker.add_argument('--index',required=True,type=int);worker.add_argument('--report',required=True,type=Path)
    args=parser.parse_args();sys.path.insert(0,'/opt/ephys-modular/maxwell/scripts')
    from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
    configure_bundled_maxwell_hdf5_plugin()
    if args.operation=='prepare':prepare(args.original,args.proof,args.output)
    elif args.operation=='check':check(args.controls,args.reports,args.original,args.proof,args.reference)
    else:one(args.controls,args.index,args.report,args.original,args.proof)
