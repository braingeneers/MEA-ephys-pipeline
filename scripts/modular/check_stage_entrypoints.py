#!/usr/bin/env python3
"""Preview the stage CLI and prove malformed units cannot start export work."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph',type=Path,required=True)
    parser.add_argument('--rejections',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--legacy-runtime',type=Path)
    args=parser.parse_args()
    source=Path(__file__).resolve().parents[2]
    root=args.output.resolve()
    root.mkdir(parents=True,exist_ok=False)
    env=os.environ.copy()
    executable='nextflow'
    if args.legacy_runtime:
        cache=args.legacy_runtime
        executable=str(cache/'nextflow-23.08.0-edge-all')
        assert hashlib.sha256(Path(executable).read_bytes()).hexdigest()=='4f667c0ce1b939133baadd18678c83651e06f4bc676112af3b42d2c940ce5ac2'
        env.update(JAVA_HOME=str(cache/'jdk-17.0.16+8-jre'),NXF_HOME=str(cache/'home'),
                   NXF_DIST=str(cache/'framework'),NXF_PLUGINS_DIR=str(cache/'plugins'),NXF_VER='23.08.0-edge')
        env.pop('NXF_SYNTAX_PARSER',None)
    config=source/'pipeline/nextflow_stage_docker.config'
    prefix=[executable,'-C',str(config),'run',str(source/'pipeline/main_stages.nf'),'-ansi-log','false']
    raw=args.graph/'data/slow'
    unit=json.loads((args.rejections/'incomplete-reported/bundle.json').read_text())['unit_id']
    checks={}

    def run(name,extra,success,needle=None):
        cwd=root/name
        cwd.mkdir()
        trace=cwd/'trace.tsv'
        command=prefix+['--results_path',str(cwd/'published'),'--torch_device','cpu','-with-trace',str(trace)]+extra
        (cwd/'command.json').write_text(json.dumps(command,indent=2)+'\n')
        with (cwd/'run.log').open('w') as log:
            result=subprocess.run(command,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=180)
        output=(cwd/'run.log').read_text()
        assert (result.returncode==0)==success,(name,output[-6000:])
        if needle:
            assert needle in output,(name,output[-6000:])
        rows=[]
        if trace.exists():
            with trace.open() as stream:
                rows=list(csv.DictReader(stream,delimiter='\t'))
        checks[name]={'exit':result.returncode,'tasks':rows}
        return rows

    run('grouped-preview',['--stage','all','--unit_id','slow','--ecephys_path',str(raw),'-preview'],True)
    for stage,kind in [('sorting','prepared'),('analysis','sorted'),('reporting','analyzed'),('export','reported')]:
        bundle=args.graph/'published/alpha'/('sorting' if kind=='sorted' else 'analysis' if kind=='analyzed' else 'reporting' if kind=='reported' else 'preprocessing')/'bundles'/('slow.'+kind)
        run(stage+'-preview',['--stage',stage,'--unit_id','slow','--input_bundle',str(bundle),'-preview'],True)
    units=root/'duplicate-units.json'
    units.write_text(json.dumps([{'metadata':{'unit_id':'duplicate'},'input':str(raw)}]*2))
    rows=run('duplicate-units',['--stage','preprocessing','--units_file',str(units)],False,'Duplicate unit identities')
    assert not rows
    rows=run('incomplete-reports',['--stage','export','--unit_id',unit,'--input_bundle',str(args.rejections/'incomplete-reported')],False,'report_summary.json')
    assert len(rows)==1 and 'VERIFY_REPORTED' in rows[0]['name'] and rows[0]['status']=='FAILED',rows
    result={'passed':True,'legacy_runtime':bool(args.legacy_runtime),'checks':checks,
            'missing_report_prevents_both_nwb_processes':True,'scientific_recomputation':False}
    (root/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'passed':True,'legacy_runtime':bool(args.legacy_runtime),'checks':list(checks)},indent=2))


if __name__=='__main__':
    main()
