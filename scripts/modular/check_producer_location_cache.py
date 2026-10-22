#!/usr/bin/env python3
"""Prove unchanged resume and recorder-only invalidation through the real dispatch port."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--legacy-runtime',type=Path)
    args=parser.parse_args()
    source=Path(__file__).resolve().parents[2];root=args.output.resolve();root.mkdir(exist_ok=False)
    snapshot=root/'source';shutil.copytree(source/'pipeline',snapshot/'pipeline')
    code=root/'capsule';code.mkdir()
    (code/'run').write_text("#!/usr/bin/env bash\npython3 - <<'PYTHON'\nfrom pathlib import Path\nPath('../results/job_0.json').write_text('{\"recording_name\":\"cache-probe\"}')\nPYTHON\n")
    (code/'get_max_recording_duration_min.py').write_text("print('3')\n")
    (root/'input').mkdir();(root/'input/sentinel.txt').write_text('unchanged\n')
    (root/'main.nf').write_text(f'''nextflow.enable.dsl=2
include {{ meaCodeInput }} from '{snapshot}/pipeline/code_identity'
include {{ processOptions; loadLaunchSettings }} from '{snapshot}/pipeline/settings'
include {{ job_dispatch }} from '{snapshot}/pipeline/modules/job_dispatch'
a=loadLaunchSettings([ecephys_path:'{root}/input',results_path:'{root}/published',torch_device:'cpu',git_repo_prefix:'https://github.com/AllenNeuralDynamics/aind-'],file('{snapshot}/pipeline'))
workflow {{
 cfg=processOptions('job_dispatch',a)+[escape_probe:(params.escape_probe ?: false)]
 job_dispatch(channel.value(cfg),channel.value(meaCodeInput(file('{code}'))),channel.value(file('{root}/input')))
 job_dispatch.out.unit_results.map{{ settings,execution,results,inputs,duration -> settings.producer_location_identity }}
 .subscribe{{ value -> file('identity.txt').text=value }}
}}
''')
    (root/'probe.config').write_text("process.executor='local'\ndocker.enabled=true\ndocker.runOptions='--user 1000:1000 --network none -e HOME=/tmp'\nprocess.cpus=1\nprocess.memory='512 MB'\ntrace.fields='task_id,hash,name,status,exit'\nparams.escape_probe=false\n")
    command=['nextflow'];env=os.environ.copy()
    if args.legacy_runtime:
        cache=args.legacy_runtime;executable=cache/'nextflow-23.08.0-edge-all'
        assert hashlib.sha256(executable.read_bytes()).hexdigest()=='4f667c0ce1b939133baadd18678c83651e06f4bc676112af3b42d2c940ce5ac2'
        command=[str(executable)];env.update(JAVA_HOME=str(cache/'jdk-17.0.16+8-jre'),NXF_HOME=str(cache/'home'),NXF_DIST=str(cache/'framework'),NXF_PLUGINS_DIR=str(cache/'plugins'),NXF_VER='23.08.0-edge');env.pop('NXF_SYNTAX_PARSER',None)
    runs=[]
    for index in range(3):
        if index==2:
            helper=snapshot/'pipeline/producer_locations.nf';text=helper.read_text()
            needle='MEA_PRODUCER_LOCATION\n';assert text.count(needle)==1
            helper.write_text(text.replace(needle,'# cache probe: recorder-only edit\n'+needle))
        cmd=command+['-C',str(root/'probe.config'),'run',str(root/'main.nf'),'-ansi-log','false','-with-trace',str(root/f'trace-{index}.tsv')]
        if index:cmd+=['-resume']
        (root/f'command-{index}.json').write_text(json.dumps(cmd,indent=2)+'\n')
        with (root/f'driver-{index}.log').open('w') as log:subprocess.run(cmd,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=120)
        rows=list(csv.DictReader((root/f'trace-{index}.tsv').open(),delimiter='\t'));assert len(rows)==1
        assert rows[0]['status']==('CACHED' if index==1 else 'COMPLETED') and rows[0]['exit']=='0'
        runs.append(dict(status=rows[0]['status'],hash=rows[0]['hash'],identity=(root/'identity.txt').read_text()))
    assert runs[0]['hash']==runs[1]['hash'] and runs[0]['hash']!=runs[2]['hash']
    assert runs[0]['identity']==runs[1]['identity'] and runs[0]['identity']!=runs[2]['identity']
    protected = root/'protected-input'; protected.mkdir()
    with (root/'probe.config').open('a') as stream:
        stream.write("params.escape_probe=true\nprocess.scratch='"+str(root/'scratch')+"'\n")
        stream.write("process.beforeScript = \'\'\'ln -s \""+str(protected)+"\" \"$(dirname \"${BASH_SOURCE[0]}\")/unit-results\"\'\'\'\n")
    cmd=command+['-C',str(root/'probe.config'),'run',str(root/'main.nf'),'-ansi-log','false','-with-trace',str(root/'trace-escape.tsv')]
    with (root/'driver-escape.log').open('w') as log:
        failed=subprocess.run(cmd,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=120)
    assert failed.returncode != 0
    assert 'Producer location output port escapes its shared task directory' in (root/'driver-escape.log').read_text()
    assert not list(protected.iterdir()), 'Recorder wrote into a protected input through an escaping output symlink'
    result=dict(passed=True,shared_output_escape_rejected=True,protected_input_unchanged=True,
                legacy_runtime=bool(args.legacy_runtime),runs=runs,
        unchanged_resume_cached=True,recorder_only_edit_invalidates_producer=True,scientific_capsule_stubbed=True)
    (root/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))


if __name__=='__main__':main()
