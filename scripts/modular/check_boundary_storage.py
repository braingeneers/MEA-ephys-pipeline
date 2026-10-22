#!/usr/bin/env python3
"""Execute the maintained bundle/verify modules with global scratch enabled.

Three small Docker tasks test storage placement, not scientific computation.
Run inside the pinned driver with the Docker socket and identical absolute mounts.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--legacy-runtime', type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    root = args.output.resolve()
    root.mkdir(exist_ok=False)
    helpers = root / 'helpers'
    helpers.mkdir()
    shutil.copy2(source / 'pipeline/helpers/portable_bundle.py', helpers)
    (helpers / 'stage_bundle.py').write_text('''import argparse,json
from pathlib import Path
from portable_bundle import write_manifest
p=argparse.ArgumentParser();p.add_argument('--outcomes-code');p.add_argument('--common-code')
s=p.add_subparsers(dest='operation');a=s.add_parser('build');a.add_argument('--request');a.add_argument('--inputs');a.add_argument('--output')
a=s.add_parser('verify');a.add_argument('--bundle');a.add_argument('--kind');a.add_argument('--unit-id');a.add_argument('--unit-meta')
a=p.parse_args()
assert Path.cwd().is_relative_to(Path(__file__).resolve().parents[1]/'work'), str(Path.cwd())
if a.operation=='build':
 root=Path(a.output);root.mkdir();write_manifest(root/'bundle.json',{'unit_id':'storage-probe','metadata':{'max_duration_minutes':'3'}})
else:
 value=json.loads((Path(a.bundle)/'bundle.json').read_text());assert value['unit_id']==a.unit_id
print(json.dumps({'operation':a.operation,'shared_work':True,'cwd':str(Path.cwd())}))
''')
    (root / 'outcomes.py').write_text('# test-only boundary storage probe\n')
    (root / 'common').mkdir()
    (root / 'common/placeholder.txt').write_text('test-only\n')
    (root / 'input.txt').write_text('explicit test-only payload\n')
    image = json.loads((source / 'pipeline/container_images.json').read_text())['base']
    (root / 'main.nf').write_text(f'''nextflow.enable.dsl=2
include {{ meaCodeInput }} from '{source}/pipeline/code_identity'
include {{ bundle_stage as PACK_PROBE }} from '{source}/pipeline/modules/bundle_stage'
include {{ verify_stage as VERIFY_PROBE }} from '{source}/pipeline/modules/verify_stage'
process SCIENCE_SCRATCH {{
 output: path 'storage-control.json'
 script:
 """
 python - <<'PYTHON'
from pathlib import Path
import json
assert not Path.cwd().is_relative_to(Path('{root}/work'))
Path('storage-control.json').write_text(json.dumps({{'shared_work':False}}))
PYTHON
 """
}}
workflow {{
 cfg=[unit_id:'storage-probe',unit_meta:[unit_id:'storage-probe'],stage:'nwb',container:'{image}',publish:false,publish_overwrite:null,results_path:'{root}/published']
 h=channel.value(meaCodeInput(file('{helpers}')))
 o=channel.value(meaCodeInput(file('{root}/outcomes.py')))
 c=channel.value(meaCodeInput(file('{root}/common')))
 PACK_PROBE(channel.of(tuple(cfg,[schema_version:1],[file('{root}/input.txt')])),h,o,c)
 VERIFY_PROBE(PACK_PROBE.out.units,h,o,c)
 SCIENCE_SCRATCH()
}}
''')
    (root / 'probe.config').write_text(f'''process.executor='local'
process.scratch='{root}/scratch'
process.container='{image}'
process.cpus=1
process.memory='1 GB'
executor.queueSize=3
docker.enabled=true
docker.runOptions='--user 1000:1000 --network none -e HOME=/tmp'
trace.fields='task_id,hash,name,status,exit,workdir'
''')
    command = ['nextflow']
    env = os.environ.copy()
    if args.legacy_runtime:
        cache = args.legacy_runtime
        executable = cache / 'nextflow-23.08.0-edge-all'
        assert hashlib.sha256(executable.read_bytes()).hexdigest() == '4f667c0ce1b939133baadd18678c83651e06f4bc676112af3b42d2c940ce5ac2'
        command = [str(executable)]
        env.update(JAVA_HOME=str(cache/'jdk-17.0.16+8-jre'),NXF_HOME=str(cache/'home'),
                   NXF_DIST=str(cache/'framework'),NXF_PLUGINS_DIR=str(cache/'plugins'),NXF_VER='23.08.0-edge')
        env.pop('NXF_SYNTAX_PARSER',None)
    command += ['-C',str(root/'probe.config'),'run',str(root/'main.nf'),'-ansi-log','false','-with-trace',str(root/'trace.tsv')]
    (root/'command.json').write_text(json.dumps(command,indent=2)+'\n')
    with (root/'driver.log').open('w') as log:
        subprocess.run(command,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=300)
    rows = list(csv.DictReader((root/'trace.tsv').open(),delimiter='\t'))
    assert len(rows)==3 and all(row['status']=='COMPLETED' and row['exit']=='0' for row in rows)
    for row in rows:
        work = Path(row['workdir'])
        script = (work/'.command.run').read_text()
        if row['name']=='SCIENCE_SCRATCH':
            assert 'nxf_mktemp' in script and 'NXF_SCRATCH="$(' in script
            assert json.loads((work/'storage-control.json').read_text())['shared_work'] is False
        else:
            assert row['name'].split(' ')[0] in ('PACK_PROBE','VERIFY_PROBE')
            assert 'NXF_SCRATCH="$(' not in script
            result=json.loads((work/'.command.out').read_text())
            assert result['shared_work'] is True
    result=dict(passed=True,tasks=3,legacy_runtime=bool(args.legacy_runtime),
                boundaries_share_outputs_directly=True,ordinary_task_retains_scratch=True,
                scientific_capsules_stubbed=True)
    (root/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
