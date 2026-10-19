"""Stdlib coordinator: retain actual Docker outcomes before owned cleanup."""
from pathlib import Path
from datetime import datetime,timezone
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import subprocess
import sys

root=graph=image=owner=pythonpath=None
read=lambda path:json.loads(path.read_text())
write=lambda path,value:path.write_text(json.dumps(value,indent=2)+'\n')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def configure(root_path,graph_path):
    global root,graph,image,owner,pythonpath
    root=Path(root_path).resolve();graph=Path(graph_path).resolve()
    owner=root/'source/pipeline/components/maxwell'
    image=read(owner/'image.json')['pinned_reference']
    if '@sha256:' not in image:raise ValueError('The owning image must be immutable')
    pythonpath=':'.join(map(str,[root/'source/tests/conversion_oracle/task_observer',owner/'runtime',owner/'components/common/python',Path('/opt/ephys-modular/maxwell/scripts')]))

def command(label,argv,scenario=None):
    name='ks4-'+root.name+'-'+label
    cmd=['docker','run','-d','--init','--name',name,'--network','none','--cpus','4','--memory','8g',
         '-e','PYTHONDONTWRITEBYTECODE=1','-e','XDG_CACHE_HOME=/tmp/private-runtime-control',
         '-e','PYTHONPATH='+pythonpath,'-v',str(root)+':'+str(root),
         '-v',str(root/'source')+':'+str(root/'source')+':ro',
         '-v',str(root/'code-v1')+':'+str(root/'code-v1')+':ro',
         '-v',str(root/'code-v1')+':/code:ro','-v',str(graph)+':'+str(graph)+':ro']
    if scenario:
        cmd+=['-e','KS4_TASK_OBSERVER_DIR='+str(Path(scenario['cwd'])/'task-observer')]
        if scenario['expected']['plugin_absent']:
            for target in ('/opt/maxwell-hdf5-plugin','/root/hdf5_plugin_path_maxwell'):
                cmd+=['-v',str(root/'empty-plugin')+':'+target+':ro']
    return cmd+['--entrypoint','python',image,*argv]

def execute(label,cmd):
    out=root/'observed'/label;out.mkdir(parents=True)
    write(out/'command.json',cmd)
    started=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    write(out/'launch.json',dict(controller_exit=started.returncode,stdout=started.stdout,stderr=started.stderr,
        worker_exit_observed=False,at_utc=datetime.now(timezone.utc).isoformat(),controller_sha256=sha(Path(__file__))))
    if started.returncode:raise RuntimeError('Docker launch failed: '+label)
    identifier=started.stdout.strip()
    print('live',label,identifier,flush=True)
    waiting=subprocess.run(['docker','wait',identifier],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    info,=json.loads(subprocess.check_output(['docker','inspect',identifier]))
    logs=subprocess.run(['docker','logs',identifier],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    (out/'worker.log').write_text(logs.stdout)
    terminal=dict(container_id=identifier,actual_state=info['State'],actual_image_id=info['Image'],
        declared_image=info['Config']['Image'],default_user=info['Config'].get('User'),
        network=info['HostConfig']['NetworkMode'],mounts=info['Mounts'],
        memory=info['HostConfig']['Memory'],nano_cpus=info['HostConfig']['NanoCpus'],
        wait_controller_exit=waiting.returncode,wait_stdout=waiting.stdout,wait_stderr=waiting.stderr,
        log_controller_exit=logs.returncode,worker_exit_observed=True,
        ended_at_utc=datetime.now(timezone.utc).isoformat())
    write(out/'terminal.json',terminal)
    assert waiting.returncode==logs.returncode==0 and not info['State']['Running']
    assert waiting.stdout.strip()==str(info['State']['ExitCode'])
    removed=subprocess.run(['docker','container','remove',identifier],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    write(out/'owned-cleanup.json',dict(collected_before_cleanup=True,owned_terminal_container_only=True,
        container_id=identifier,controller_exit=removed.returncode,stdout=removed.stdout,stderr=removed.stderr))
    assert removed.returncode==0
    print('terminal',label,info['State']['ExitCode'],flush=True)
    return terminal

def run(row):
    path=Path(row['path']);assert sha(path)==row['sha256']
    scenario=read(path)
    terminal=execute(row['label'],command(row['label'],['/code/task_observer/runtime_child.py',str(path)],scenario))
    return dict(label=row['label'],actual_exit=terminal['actual_state']['ExitCode'],expected_exit=scenario['expected']['exit'],
                exit_matches=terminal['actual_state']['ExitCode']==scenario['expected']['exit'])

def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('prepare','cases'))
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--graph-root',required=True,type=Path)
    args=parser.parse_args();configure(args.root,args.graph_root)
    if args.operation=='prepare':
        result=execute('prepare',command('prepare',['/code/runtime_controls.py','prepare','--root',str(root),'--graph-root',str(graph)]))
        raise SystemExit(result['actual_state']['ExitCode'])
    prepared=read(root/'prepared.json')
    if Path(prepared['graph_root']).resolve()!=graph:raise ValueError('Graph root differs from the frozen preparation')
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,prepared['scenarios']))
    write(root/'case-controller-results.json',dict(all_actual_exits_match=all(r['exit_matches'] for r in results),cases=results,
        semantic_scientific_rejection_not_inferred_from_exit_alone=True,max_concurrent_workers=2))
    print('all case containers terminal and retained',len(results),flush=True)


if __name__=='__main__':main()
