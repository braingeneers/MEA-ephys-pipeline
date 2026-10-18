#!/usr/bin/env python3
"""Check shared image settings, prior scientific arguments and resolved backends.

Run in the pinned Nextflow driver. --baseline-source is an exact pre-integration
export, not a mutable checkout. No scientific task, Shifter or cluster work runs.
Actual image execution and alias isolation have separate process/graph probes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-source', type=Path, required=True)
    parser.add_argument('--legacy-runtime', type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    raw = root / 'input'
    raw.mkdir()
    runtime = json.loads((source / 'containers/runtime-images.json').read_text())
    images = json.loads((source / 'pipeline/container_images.json').read_text())
    assert {k: v['reference'] for k, v in runtime['images'].items()} == images
    helper = json.loads((source / 'scripts/nrp/images.json').read_text())
    assert {k: helper[k] for k in images} == images
    overrides = {k: v['local_image_id'] for k, v in runtime['images'].items()}
    cases = [{}, {'runmode': 'fast'}, {'runmode': 'spikesort'},
             {'sorter': 'kilosort25'}, {'sorter': 'spykingcircus2'}, {'sorter': 'lupin'},
             {'preprocessing_args': '--motion skip', 'spikesorting_args': '--skip-motion-correction'},
             {'params_file': str(source / 'scripts/params_no_motion.json')},
             {'params_file': str(source / 'scripts/params_no_motion.json'), 'torch_device': 'cpu'}]
    common = {'ecephys_path': str(raw), 'results_path': str(root / 'published'),
              'git_repo_prefix': 'https://github.com/AllenNeuralDynamics/aind-'}
    (root / 'cases.json').write_text(json.dumps([dict(common, **case) for case in cases]))
    (root / 'overrides.json').write_text(json.dumps(overrides))
    text = '''nextflow.enable.dsl=2
include { loadLaunchSettings; processOptions } from @SETTINGS@
include { boundaryOptions } from @STAGES@
include { loadLaunchSettings as oldLaunch; processOptions as oldOptions } from @OLD@
def read(path) { new groovy.json.JsonSlurper().parse(new File(path)) }
def images = read(@IMAGES@)
def overrides = read(@OVERRIDES@)
def results = []
def names = ['job_dispatch','preprocessing','spikesort_kilosort25','spikesort_kilosort4',
 'spikesort_spykingcircus2','spikesort_lupin','postprocessing','curation','visualization',
 'results_collector','quality_control','quality_control_collector','nwb_ecephys','nwb_units',
 'report_generation','burst_detection']
read(@CASES@).eachWithIndex { parameters, i ->
 def current=loadLaunchSettings(parameters,file(@PIPELINE@))
 def previous=oldLaunch(parameters,file(@OLD_PIPELINE@))
 assert current.findAll { k,v -> !(k in ['container_images','report_container_image']) } ==
        previous.findAll { k,v -> k!='report_container_image' }
 names.each { name ->
  def before=oldOptions(name,previous), after=processOptions(name,current)
  assert before.findAll { k,v -> k!='container' } == after.findAll { k,v -> k!='container' }
  def role=name in ['nwb_ecephys','nwb_units'] ? 'nwb' : name=='spikesort_kilosort4' ? 'ks4' : name=='report_generation' ? 'analysis' : 'base'
  assert after.container == (name=='spikesort_kilosort25' ? before.container : images[role])
 }
 def scoped=loadLaunchSettings(parameters+[container_images:overrides],file(@PIPELINE@))
 assert scoped.container_images==overrides && current.container_images==images
 def legacy=loadLaunchSettings(parameters+[container_images:overrides+[analysis:overrides.base],
                         report_container_image:overrides.analysis],file(@PIPELINE@))
 assert processOptions('report_generation',legacy).container==overrides.analysis
 assert boundaryOptions('prepared',scoped,[output_uri:@PUBLISHED@,workflow_version:'0.2.15'],[unit_id:'probe']).container==overrides.base
 scoped.container_images.base='test-only-mutated-image'
 assert current.container_images==images && overrides.base!='test-only-mutated-image'
 results.add([case:i,processes:names.size(),scientific_settings_and_nonimage_options_equal:true,
              alternative_sorter_image_preserved:true,image_maps_isolated:true])
}
file('settings.json').text=groovy.json.JsonOutput.toJson(results)
workflow {}
'''
    replacements = {'@SETTINGS@': source / 'pipeline/settings', '@STAGES@': source / 'pipeline/stages',
                    '@OLD@': args.baseline_source / 'pipeline/settings', '@IMAGES@': source / 'pipeline/container_images.json',
                    '@OVERRIDES@': root / 'overrides.json', '@CASES@': root / 'cases.json',
                    '@PIPELINE@': source / 'pipeline', '@OLD_PIPELINE@': args.baseline_source / 'pipeline',
                    '@PUBLISHED@': root / 'published'}
    for key, value in replacements.items():
        text = text.replace(key, json.dumps(str(value)))
    (root / 'settings.nf').write_text(text)
    env = os.environ.copy()
    env.update(RESULTS_PATH=str(root / 'results'), DATA_PATH=str(raw), DATA_DIR=str(raw),
               PROJECT_DIR=str(root), TMPDIR=str(root / 'tmp'), TEST_PVC='local-config-inspection',
               TEST_NAMESPACE='braingeneers', TEST_ID='ks4-profile-inspection')
    (root / 'tmp').mkdir()
    executable = 'nextflow'
    if args.legacy_runtime:
        cache = args.legacy_runtime
        executable = str(cache / 'nextflow-23.08.0-edge-all')
        assert hashlib.sha256(Path(executable).read_bytes()).hexdigest() == '4f667c0ce1b939133baadd18678c83651e06f4bc676112af3b42d2c940ce5ac2'
        env.update(JAVA_HOME=str(cache / 'jdk-17.0.16+8-jre'), NXF_HOME=str(cache / 'home'),
                   NXF_DIST=str(cache / 'framework'), NXF_PLUGINS_DIR=str(cache / 'plugins'), NXF_VER='23.08.0-edge')
        env.pop('NXF_SYNTAX_PARSER', None)
    checks = {}

    def run(name, command, success=True, needle=None):
        folder = root / name
        folder.mkdir()
        (folder / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
        with (folder / 'run.log').open('w') as log:
            completed = subprocess.run(command, cwd=folder, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=180)
        output = (folder / 'run.log').read_text()
        checks[name] = {'exit': completed.returncode, 'scientific_tasks': 0}
        assert (completed.returncode == 0) == success, (name, output[-6000:])
        if needle:
            assert needle in output, (name, output[-6000:])
        return output

    run('settings-parity', [executable, 'run', str(root / 'settings.nf'), '-ansi-log', 'false'])
    settings = json.loads((root / 'settings-parity/settings.json').read_text())
    assert len(settings) == len(cases)
    configs = {'local': source / 'pipeline/nextflow_local.config',
               'docker': source / 'pipeline/nextflow_stage_docker.config',
               'nersc': source / 'pipeline/nextflow_nersc_template.config',
               'nrp': source / 'pipeline/nextflow_nrp.config'}
    if args.legacy_runtime:
        # nf-k8s1.5.4 requires Nextflow26. Retain every actual selector, but do
        # not misrepresent this old-parser local check as a cluster execution.
        nrp = (source / 'pipeline/nextflow_nrp.config').read_text()
        assert nrp.count("plugins { id 'nf-k8s@1.5.4' }") == 1
        configs['nrp'] = root / 'nrp-selectors-without-plugin.config'
        configs['nrp'].write_text(nrp.replace("plugins { id 'nf-k8s@1.5.4' }", ''))
    for name, config in configs.items():
        output = run('config-' + name, [executable, '-C', str(config), 'config', '-flat'])
        if name == 'nersc':
            assert 'shifter.enabled = true' in output and 'nf-k8s' not in output and 'beforeScript' not in output
        elif name in ('docker', 'nrp'):
            assert output.count('task_settings.container') >= 3
        if name == 'nrp':
            assert "k8s.computeResourceType = 'Job'" in output and 'maxRetries = 0' in output
            assert 'task_settings.resources' in output and 'nrp_resources' in output
    for entry in ('main_multi_backend', 'main_stages'):
        launch = [executable, '-C', str(configs['docker']), 'run', str(source / 'pipeline' / (entry + '.nf')),
                  '--ecephys_path', str(raw), '--results_path', str(root / 'published'), '--unit_id', 'probe', '-preview', '-ansi-log', 'false']
        run(entry + '-preview', launch)
        invalid = [('roles', {'container_images': {'unknown': 'example/image:1'}}, 'Unknown container_images roles'),
                   ('map', {'container_images': 'image:1'}, 'container_images must be a map'),
                   ('empty', {'container_images': {'base': ''}}, 'container_images.base must be a non-empty'),
                   ('type', {'container_images': {'nwb': 3}}, 'container_images.nwb must be a non-empty'),
                   ('report', {'report_container_image': ''}, 'container_images.analysis must be a non-empty')]
        for label, parameters, needle in invalid:
            params = root / (entry + '-' + label + '.json')
            params.write_text(json.dumps(parameters))
            run(entry + '-reject-' + label, launch + ['-params-file', str(params)], False, needle)
    result = {'passed': True, 'legacy_runtime': bool(args.legacy_runtime), 'settings_cases': len(settings),
              'process_settings_comparisons': sum(x['processes'] for x in settings), 'checks': checks,
              'nersc_shifter_executed': False, 'cluster_work_submitted': False, 'scientific_computation': False,
              'old_parser_nrp_plugin_omitted': bool(args.legacy_runtime)}
    (root / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'checks'}, indent=2))


if __name__ == '__main__':
    main()
