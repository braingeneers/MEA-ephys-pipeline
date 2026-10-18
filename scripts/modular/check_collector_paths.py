#!/usr/bin/env python3
"""Replay actual pinned/repaired collectors on real direct and portable inputs.

Run in the pinned scientific base with input acquisitions/tasks mounted read-only.
This is collection/serialization validation, not scientific recomputation.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

SOURCE=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(SOURCE/'pipeline/helpers'))
import portable_bundle as portable


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_case(root,name,code,data,raw_base,expect_recording):
    import spikeinterface as si
    import zarr

    case=root/name
    (case/'capsule').mkdir(parents=True)
    shutil.copytree(code,case/'capsule/code')
    (case/'capsule/data').symlink_to(data,target_is_directory=True)
    (case/'capsule/results').mkdir()
    (case/'capsule/scratch').mkdir()
    publication=case/'published'
    command=['python','run_capsule.py','--pipeline-data-path',str(raw_base),
             '--pipeline-results-path',str(publication)]
    (case/'command.json').write_text(json.dumps(command,indent=2)+'\n')
    start=time.monotonic()
    with (case/'capsule.log').open('w') as log:
        p=subprocess.run(command,cwd=case/'capsule/code',stdout=log,stderr=subprocess.STDOUT,timeout=180)
    assert p.returncode==0,(name,(case/'capsule.log').read_text()[-6000:])
    result={'collector_exit':p.returncode,'duration_seconds':time.monotonic()-start,
            'analyzers':{},'preprocessed':{}}
    for path in sorted((case/'capsule/results/postprocessed').glob('*.zarr')):
        base=publication/'postprocessed'/path.name
        descriptor={'format':'analyzer_zarr','source':str(path),'base':str(base),'path':'data/'+path.name}
        try:
            identity=portable.fingerprint(descriptor,case)
        except (RuntimeError,FileNotFoundError) as error:
            assert not expect_recording,(name,str(error))
            result['analyzers'][path.name]={'recording_readable':False,'error':str(error)}
            continue
        assert expect_recording,name+' unexpectedly readable'
        assert identity['sorting']['unit_ids'] and identity['sorting']['spikes']['shape'][0]==40440
        recording=si.load(zarr.open(str(path),mode='r')['recording'][0],base_folder=base)
        analyzer=si.SortingAnalyzer.load_from_zarr(path,recording=recording)
        assert analyzer.get_num_units()==15
        qc=analyzer.sorting.get_property('default_qc')
        assert qc is not None and qc.dtype.kind=='b' and len(qc)==15
        result['analyzers'][path.name]={'recording_readable':True,'fingerprint':identity,
                                       'default_qc':qc.tolist()}
    assert len(result['analyzers'])==1
    for path in sorted((case/'capsule/results/preprocessed').glob('*.json')):
        rec=si.load(path,base_folder=raw_base)
        result['preprocessed'][path.name]=portable.recording_identity(rec)
    assert len(result['preprocessed'])==1
    (case/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original-code',type=Path,required=True)
    parser.add_argument('--retained-run',type=Path,required=True)
    parser.add_argument('--raw',type=Path,required=True)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    fixed=SOURCE/'capsules/results_collector/code'
    old=(args.original_code/'run_capsule.py').read_text()
    expected=old.replace('if "ecephys_session" in recording_dict_str:',
                         'if pipeline_data_path is None and "ecephys_session" in recording_dict_str:',1)
    before='            recording_dict_str = json.dumps(recording_dict_mapped, indent=4)\n            recording_dict_str = recording_dict_str.replace("ecephys_session", session_name)'
    after='            recording_dict_str = json.dumps(recording_dict_mapped, indent=4)\n            if pipeline_data_path is None:\n                recording_dict_str = recording_dict_str.replace("ecephys_session", session_name)'
    assert before in expected
    expected=expected.replace(before,after,1)
    expected='\n'.join(line.rstrip() for line in expected.splitlines())+'\n'
    assert (fixed/'run_capsule.py').read_text()==expected,'Unexpected scientific collector source change'
    assert (fixed/'run').read_bytes()==(args.original_code/'run').read_bytes()
    assert (fixed.parent/'LICENSE').read_bytes()==(args.original_code.parent/'LICENSE').read_bytes()
    with (args.retained_run/'evidence/trace.tsv').open() as stream:
        row=next(row for row in csv.DictReader(stream,delimiter='\t') if row['name'].split(' (')[0]=='results_collector')
    prefix,suffix=row['hash'].split('/')
    tasks=list((args.retained_run/'work'/prefix).glob(suffix+'*'))
    assert len(tasks)==1
    cases={}
    for layout,data,raw_base in [('portable',args.bundle/'data',args.bundle/'data/ecephys_session'),
                               ('direct',tasks[0]/'capsule/data',args.raw)]:
        for implementation,code in [('upstream',args.original_code),('fixed',fixed)]:
            name=layout+'-'+implementation
            cases[name]=run_case(args.output,name,code,data,raw_base,
                                 expect_recording=(name!='portable-upstream'))
            print(name,'PASS',flush=True)
    def scientific(case):
        return {'analyzers':{k:{'fingerprint':v['fingerprint'],'default_qc':v['default_qc']}
                             for k,v in case['analyzers'].items()},'preprocessed':case['preprocessed']}
    assert scientific(cases['direct-upstream'])==scientific(cases['direct-fixed'])
    assert scientific(cases['direct-upstream'])==scientific(cases['portable-fixed'])
    # Rebinding a formerly valid graph to a missing dependency stays a hard load
    # failure; a permissive analyzer load alone cannot certify collection.
    import spikeinterface as si
    import zarr
    path=next((args.output/'portable-fixed/capsule/results/postprocessed').glob('*.zarr'))
    value=zarr.open(str(path),mode='r')['recording'][0]
    from copy import deepcopy
    from spikeinterface.core.core_tools import extractor_dict_iterator,set_value_in_extractor_dict
    wrong=deepcopy(value)
    count=0
    for item in list(extractor_dict_iterator(wrong)):
        if item.name in ('file_path','folder_path'):
            set_value_in_extractor_dict(wrong,item.access_path,'missing-recording-dependency')
            count+=1
    assert count
    try:
        si.load(wrong,base_folder=args.output)
    except (RuntimeError,FileNotFoundError):
        missing_rejected=True
    else:
        raise AssertionError('Missing recording accepted')
    result={'passed':True,'scientific_recomputation':False,'actual_capsule_replays':4,
            'only_behavior_changes_two_explicit_path_guards':True,'upstream_trailing_whitespace_normalized':True,
            'original_license_and_runner_unchanged':True,
            'original_portable_failure_reproduced':True,'direct_and_portable_science_identical':True,
            'legacy_implicit_path_branch_retained':True,'missing_dependency_rejected':missing_rejected,
            'original_code_sha256':sha(args.original_code/'run_capsule.py'),
            'fixed_code_sha256':sha(fixed/'run_capsule.py'),
            'cases':{name:{'analyzers':list(value['analyzers']), 'elapsed_seconds':value['duration_seconds']}
                     for name,value in cases.items()}}
    (args.output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
