#!/usr/bin/env python3
"""Render local Docker resource commands using tiny non-scientific NF tasks.

Run in the pinned NF26 driver with equal absolute source/evidence mounts. A test
beforeScript exits71 before Docker launch; .command.run is the rendering oracle.
No scientific container, GPU workload, dataset or signal computation is started.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import shlex
import subprocess


IMAGE = 'braingeneers/mea-ephys-base@sha256:32df3345e37711855626eb25a80b98bcd9fc718c1aa95279b42f14732f60b542'
OVERRIDE = '134217729 B'  #128MiB+1 byte tests NF Docker's whole-MiB rounding.
PROFILES = {
    'stage': ('nextflow_stage_docker.config', 8192),
    'full': ('nextflow_full_docker.config', 8192),
    'legacy': ('nextflow_local.config', 32768),
}


def option(tokens, name):
    values = []
    for index, token in enumerate(tokens):
        if token == name:
            values.append(tokens[index + 1])
        elif token.startswith(name + '='):
            values.append(token.split('=', 1)[1])
    assert len(values) == 1, (name, values)
    return values[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    root = args.output.resolve()
    root.mkdir(exist_ok=False)
    version = subprocess.run(['nextflow', '-version'], capture_output=True, text=True, check=True)
    assert '26.04.6' in version.stdout + version.stderr, version.stdout + version.stderr
    (root / 'nextflow-version.txt').write_text(version.stdout + version.stderr)
    cases = [(profile, 'postprocessing', mode) for profile in PROFILES for mode in ['default', 'override']]
    cases += [(profile, 'spikesort_kilosort4', 'override') for profile in PROFILES]
    cases += [('legacy', 'spikesort_kilosort25', 'override')]
    results = []
    for profile, role, mode in cases:
        run = root / (profile + '-' + role + '-' + mode)
        run.mkdir()
        settings = {'container': IMAGE, 'torch_device': 'cuda', 'resources': {}}
        if mode == 'override' and profile != 'legacy':
            settings['resources']['memory'] = OVERRIDE
        script = '''nextflow.enable.dsl=2
params.role = null
params.fixture_settings = null
'''
        for name in ['postprocessing', 'spikesort_kilosort4', 'spikesort_kilosort25']:
            script += f'''process {name} {{
 container {{ task_settings.container }}
 input: val task_settings
 script:
 def contract=[cpus:task.cpus,memory:task.memory.toBytes(),accelerator:task.accelerator ? [request:task.accelerator.request,limit:task.accelerator.limit] : null]
 """
 # RENDERED_RESOURCE_CONTRACT: ${{groovy.json.JsonOutput.toJson(contract)}}
 true
 """
}}
'''
        script += 'workflow {\n'
        for name in ['postprocessing', 'spikesort_kilosort4', 'spikesort_kilosort25']:
            script += f" if (params.role == '{name}') {name}(channel.value(params.fixture_settings))\n"
        script += '}\n'
        (run / 'main.nf').write_text(script)
        (run / 'params.json').write_text(json.dumps({'role': role, 'fixture_settings': settings}))
        memory_directive = f"memory='{OVERRIDE}'" if mode == 'override' and profile == 'legacy' else ''
        overlay = f'''executor.cpus=16
executor.memory='64 GB'
process {{ withName: '{role}' {{
 beforeScript='echo DOCKER_RENDER_ONLY_SENTINEL >&2; exit 71'
 {memory_directive}
}} }}
trace.fields='task_id,hash,name,status,exit,workdir'
'''
        if mode == 'override':
            base = {'stage': '--user 1000:1000 -e HOME=/tmp -e MPLBACKEND=Agg',
                    'full': '', 'legacy': '--volume $DATA_PATH:/tmp/data'}[profile]
            options = (base + ' --label portable-shm-check=true --network none').replace('\\', '\\\\').replace("'", "\\'")
            overlay += "docker.runOptions='" + options + "'\n"
        (run / 'overlay.config').write_text(overlay)
        env = os.environ.copy()
        env.update(RESULTS_PATH=str(run / 'published'), DATA_PATH=str(run / 'input'))
        command = ['nextflow', '-C', str(source / 'pipeline' / PROFILES[profile][0]) + ',' + str(run / 'overlay.config'),
                   'run', str(run / 'main.nf'), '-lib', str(source / 'pipeline/components/maxwell/lib'),
                   '-params-file', str(run / 'params.json'), '-ansi-log', 'false', '-with-trace', str(run / 'trace.tsv')]
        (run / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
        completed = subprocess.run(command, cwd=run, env=env, capture_output=True, timeout=120)
        (run / 'driver.stdout').write_bytes(completed.stdout)
        (run / 'driver.stderr').write_bytes(completed.stderr)
        assert completed.returncode != 0, 'Render sentinel must stop before Docker launch'
        rows = list(csv.DictReader((run / 'trace.tsv').open(), delimiter='\t'))
        assert len(rows) == 1 and rows[0]['status'] == 'FAILED' and rows[0]['exit'] == '71', rows
        work = Path(rows[0]['workdir'])
        assert (work / '.exitcode').read_text().strip() == '71'
        assert 'DOCKER_RENDER_ONLY_SENTINEL' in (work / '.command.log').read_text()
        docker_lines = [line for line in (work / '.command.run').read_text().splitlines() if 'docker run ' in line]
        assert len(docker_lines) == 1, (profile, role, mode, 'Expected one rendered Docker launch')
        docker_line = docker_lines[0]
        tokens = shlex.split(docker_line)
        expected_mb = 128 if mode == 'override' else PROFILES[profile][1]
        assert option(tokens, '--memory') == option(tokens, '--shm-size') == str(expected_mb) + 'm', docker_line
        assert IMAGE in tokens
        if profile in ['stage', 'full']:
            assert option(tokens, '--user') == '1000:1000'
            assert 'HOME=/tmp' in tokens and 'MPLBACKEND=Agg' in tokens
        else:
            assert '--volume' in tokens and '$DATA_PATH:/tmp/data' in tokens
            if role == 'postprocessing':
                assert option(tokens, '--memory-swap') == '-1' and option(tokens, '--memory-swappiness') == '20'
        if role.startswith('spikesort_'):
            contract_line = next(line for line in (work / '.command.sh').read_text().splitlines()
                                 if '# RENDERED_RESOURCE_CONTRACT: ' in line)
            contract = json.loads(contract_line.split('# RENDERED_RESOURCE_CONTRACT: ', 1)[1])
            if profile == 'legacy':
                assert option(tokens, '--gpus') == 'all', docker_line
            else:
                assert contract['accelerator'] == dict(request=1, limit=1), contract
                assert all(type(value) is int for value in contract['accelerator'].values())
                # Original c509 stage/full rendering also lacks a Docker GPU flag.
                assert not any(token.startswith('--gpus') for token in tokens), docker_line
        if mode == 'override':
            assert option(tokens, '--label') == 'portable-shm-check=true' and option(tokens, '--network') == 'none'
        result = dict(profile=profile,role=role,mode=mode,memory_and_shm=str(expected_mb)+'m',
                      override_rounds_with_Docker_memory=mode=='override',existing_options_preserved=True,
                      rendered_command=docker_line,trace=rows[0],task_stopped_before_Docker=True)
        if role.startswith('spikesort_'):
            result.update(accelerator_contract=contract['accelerator'],
                          local_Docker_GPU_admission_qualified=False,
                          GPU_scope='Original typed declaration/legacy flag preserved; no GPU workload executed')
        (run / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        results.append(result)
    result = dict(passed=True,cases=results,Nextflow='26.04.6',scientific_replay=False,
                  Docker_tasks_started=0,GPU_workloads_started=0,scope='Actual pinned Nextflow .command.run rendering; fixture preflight exits71 before Docker launch.')
    (root / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(dict(passed=True,cases=len(results),Nextflow='26.04.6',Docker_tasks_started=0)))


if __name__ == '__main__':
    main()
