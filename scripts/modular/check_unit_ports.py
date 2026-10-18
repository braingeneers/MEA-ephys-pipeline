#!/usr/bin/env python3
"""Probe real process ports, scoped resource selectors and two imported aliases.

Scientific capsules are tiny argv-recording stubs. Docker mode executes the exact
task images. Metadata mode executes only host stubs and inspects CPU/CUDA allocation
bindings; it never performs CUDA computation or submits a Kubernetes task.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


THREAD_KEYS = ("CO_CPUS", "N_JOBS_EXT", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("docker", "metadata"), default="docker")
    parser.add_argument("--legacy-runtime", type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    images = json.loads((source / "scripts/nrp/images.json").read_text())
    analysis = json.loads((source / "containers/analysis/image.json").read_text())["local_image_id"]
    for unit in ("slow", "fast", "job-probe"):
        path = root / "data" / unit / "prepared"
        path.mkdir(parents=True)
        (path / "sentinel.txt").write_text(unit)
    code = root / "sorter-code"
    code.mkdir()
    (code / "run").write_text("""#!/usr/bin/env bash
exec python3 - "$@" <<'PYTHON'
import json,os,sys,time
from pathlib import Path
args=sys.argv[1:]; params=json.loads(args[args.index('--params')+1])
sentinels=[p/'sentinel.txt' for p in Path('../data').iterdir() if p.is_dir() and (p/'sentinel.txt').is_file()]
assert len(sentinels)==1
unit=sentinels[0].read_text()
if unit=='slow': time.sleep(3)
result={'unit':unit,'params':params,'argv':args,'threads':{key:os.environ.get(key) for key in
 ('CO_CPUS','N_JOBS_EXT','OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMBA_NUM_THREADS')}}
Path('../results/arguments.json').write_text(json.dumps(result))
PYTHON
""")
    report_code = root / "report-code"
    report_code.mkdir()
    (report_code / "run_capsule.py").write_text("""import argparse,json,os
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--analyzer-dir');p.add_argument('--output-dir');p.add_argument('--thresholds')
a=p.parse_args(); data=json.loads((Path(a.analyzer_dir)/'arguments.json').read_text())
assert data['unit']==Path(a.output_dir).name, 'unit/recording association changed'
data['report_threads']={key:os.environ.get(key) for key in
 ('CO_CPUS','N_JOBS_EXT','OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMBA_NUM_THREADS')}
(Path(a.output_dir)/'probe.json').write_text(json.dumps(data))
""")
    job_code = root / "job-code"
    job_code.mkdir()
    (job_code / "run").write_text("""#!/usr/bin/env bash
exec python3 - <<'PYTHON'
import json
from pathlib import Path
unit=(Path('../data/ecephys_session')/'sentinel.txt').read_text()
Path('../results/job_0.json').write_text(json.dumps({'recording_name':unit}))
PYTHON
""")
    (job_code / "get_max_recording_duration_min.py").write_text("print('7.5')\n")
    launches = {}
    for alias, cpus in (("alpha", 1), ("beta", 2)):
        device = "cuda" if args.mode == "metadata" and alias == "beta" else "cpu"
        params = {"spikesorting": {"sorter": "kilosort4", "kilosort4": {"sorter": {"torch_device": device, "probe_marker": alias}}}}
        params_path = root / (alias + "-params.json")
        params_path.write_text(json.dumps(params))
        resources = {"spikesort_kilosort4": {"cpus": cpus, "memory": "512 MB" if cpus == 1 else "768 MB",
                                           "time": "10 min" if cpus == 1 else "11 min", "disk": "128 MB" if cpus == 1 else "256 MB"},
                     "job_dispatch": {"cpus": 1}}
        if alias == "beta":
            resources["report_generation"] = {"cpus": 2, "memory": "768 MB", "time": "11 min", "disk": "256 MB"}
        launches[alias] = {"executor": "k8s", "ecephys_path": str(root / "data"), "results_path": str(root / "published" / alias),
                           "params_file": str(params_path), "torch_device": device, "report_container_image": analysis,
                           "git_repo_prefix": "https://github.com/AllenNeuralDynamics/aind-", "publish_overwrite": True,
                           "process_resources": resources}
        (root / (alias + "-launch.json")).write_text(json.dumps(launches[alias]))
    component = r'''nextflow.enable.dsl=2
include { processOptions } from @SETTINGS@
include { spikesort_kilosort4 } from @SORTER@
include { report_generation } from @REPORT@
workflow PROBE_STAGE {
    take: units; launch
    main:
    calls = units.map { unit, prepared -> tuple(processOptions('spikesort_kilosort4', launch) + [unit_id:unit], prepared) }
    spikesort_kilosort4(calls.map{ cfg, prepared -> cfg }, channel.value(file(@SORT_CODE@)),
        channel.value('1'), calls.map{ cfg, prepared -> prepared })
    reports = spikesort_kilosort4.out.unit_results.map { cfg, execution, results, layout ->
        tuple(processOptions('report_generation', launch) + [unit_id:cfg.unit_id], tuple(cfg.unit_id, results, [layout])) }
    report_generation(reports.map{ cfg, analyzer -> cfg }, reports.map{ cfg, analyzer -> analyzer }, channel.value(file(@REPORT_CODE@)))
    emit:
    units = report_generation.out.unit_results
    sorting = spikesort_kilosort4.out.unit_results
}
'''
    replacements = {"@SETTINGS@": source / "pipeline/settings", "@SORTER@": source / "pipeline/modules/spikesort_kilosort4",
                    "@REPORT@": source / "pipeline/modules/report_generation", "@SORT_CODE@": code, "@REPORT_CODE@": report_code}
    for key, path in replacements.items():
        component = component.replace(key, json.dumps(str(path)))
    (root / "component.nf").write_text(component)
    script = r'''nextflow.enable.dsl=2
include { loadLaunchSettings; processOptions } from @SETTINGS@
include { job_dispatch } from @DISPATCH@
include { PROBE_STAGE as ALPHA; PROBE_STAGE as BETA } from './component'
a = loadLaunchSettings(new groovy.json.JsonSlurper().parse(new File(@ALPHA@)), @PIPELINE@)
b = loadLaunchSettings(new groovy.json.JsonSlurper().parse(new File(@BETA@)), @PIPELINE@)
workflow {
    left = channel.of(tuple('slow',file(@SLOW@)),tuple('fast',file(@FAST@)))
    right = channel.of(tuple('slow',file(@SLOW@)),tuple('fast',file(@FAST@)))
    ALPHA(left,a); BETA(right,b)
    alpha_units = ALPHA.out.units.map{ cfg, execution, recording, results, layout ->
        [alias:'alpha',unit:cfg.unit_id,recording:recording,results:results.toString(),layout:layout.toString(),execution:execution] }
    beta_units = BETA.out.units.map{ cfg, execution, recording, results, layout ->
        [alias:'beta',unit:cfg.unit_id,recording:recording,results:results.toString(),layout:layout.toString(),execution:execution] }
    alpha_units.mix(beta_units).collect().subscribe { rows -> file('emitted-reports.json').text=groovy.json.JsonOutput.toJson(rows) }
    alpha_sort = ALPHA.out.sorting.map{ cfg, execution, results, layout ->
        [alias:'alpha',unit:cfg.unit_id,results:results.toString(),layout:layout.toString(),execution:execution] }
    beta_sort = BETA.out.sorting.map{ cfg, execution, results, layout ->
        [alias:'beta',unit:cfg.unit_id,results:results.toString(),layout:layout.toString(),execution:execution] }
    alpha_sort.mix(beta_sort).collect().subscribe { rows -> file('emitted-sorting.json').text=groovy.json.JsonOutput.toJson(rows) }
    job_dispatch(channel.value(processOptions('job_dispatch',a)+[unit_id:'job-probe']),channel.value(file(@JOB_CODE@)),channel.value(file(@JOB_INPUT@)))
    job_dispatch.out.unit_results.subscribe { cfg, execution, results, layout, duration ->
        file('emitted-job.json').text=groovy.json.JsonOutput.toJson([unit:cfg.unit_id,execution:execution,
            results:results.toString(),layout:layout.toString(),duration:duration.text.trim()]) }
}
'''
    replacements.update({"@DISPATCH@": source / "pipeline/modules/job_dispatch", "@ALPHA@": root / "alpha-launch.json",
                         "@BETA@": root / "beta-launch.json", "@PIPELINE@": source / "pipeline",
                         "@SLOW@": root / "data/slow/prepared", "@FAST@": root / "data/fast/prepared",
                         "@JOB_CODE@": job_code, "@JOB_INPUT@": root / "data/job-probe/prepared"})
    for key, path in replacements.items():
        script = script.replace(key, json.dumps(str(path)))
    (root / "main.nf").write_text(script)
    nrp = source / "pipeline/nextflow_nrp.config"
    env = os.environ.copy()
    executable = "nextflow"
    if args.legacy_runtime:
        cache = args.legacy_runtime
        executable = str(cache / "nextflow-23.08.0-edge-all")
        assert hashlib.sha256(Path(executable).read_bytes()).hexdigest() == "4f667c0ce1b939133baadd18678c83651e06f4bc676112af3b42d2c940ce5ac2"
        env.update(JAVA_HOME=str(cache / "jdk-17.0.16+8-jre"), NXF_HOME=str(cache / "home"), NXF_DIST=str(cache / "framework"),
                   NXF_PLUGINS_DIR=str(cache / "plugins"), NXF_VER="23.08.0-edge")
        env.pop("NXF_SYNTAX_PARSER", None)
        # Exercise the actual selectors under the old parser, without loading a
        # Kubernetes plugin that requires Nextflow 26. This is a local-only probe.
        nrp = root / "nrp-selectors-without-plugin.config"
        text = (source / "pipeline/nextflow_nrp.config").read_text().replace("plugins { id 'nf-k8s@1.5.4' }", "")
        nrp.write_text(text)
    if args.mode == "metadata":
        shim = root / "bin"
        shim.mkdir()
        (shim / "python").symlink_to(sys.executable)
        env["PATH"] = str(shim) + os.pathsep + env["PATH"]
    overlay = root / "local.config"
    overlay.write_text("process.executor='local'\nexecutor.queueSize=8\nshifter.enabled=false\ndocker.enabled=" + str(args.mode == "docker").lower() + "\n"
                       "docker.runOptions='--user 1000:1000 -e HOME=/tmp'\n"
                       "docker.envWhitelist=" + json.dumps(list(THREAD_KEYS)) + "\n"
                       "params.nrp_resources=[spikesort_kilosort4:[cpus:7],report_generation:[cpus:3,memory:'896 MB',time:'12 min',disk:'384 MB']]\n")
    command = [executable, "-C", str(nrp) + "," + str(overlay), "run", str(root / "main.nf"), "-profile",
               "gpu" if args.mode == "docker" else "cpu", "-with-trace", str(root / "trace.tsv")]
    (root / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    with (root / "run.log").open("w") as log:
        subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600)
    sorting = json.loads((root / "emitted-sorting.json").read_text())
    reports = json.loads((root / "emitted-reports.json").read_text())
    job = json.loads((root / "emitted-job.json").read_text())
    assert len(sorting) == len(reports) == 4
    assert {(row["alias"], row["unit"]) for row in sorting} == {(alias, unit) for alias in ("alpha", "beta") for unit in ("slow", "fast")}
    for alias in ("alpha", "beta"):
        for unit in ("slow", "fast"):
            row = next(row for row in sorting if (row["alias"], row["unit"]) == (alias, unit))
            data = json.loads((Path(row["results"]) / "arguments.json").read_text())
            cpus = 1 if alias == "alpha" else 2
            device = "cuda" if args.mode == "metadata" and alias == "beta" else "cpu"
            assert data["unit"] == unit and data["params"]["sorter"] == {"torch_device": device, "probe_marker": alias}
            assert data["threads"] == {key: str(cpus) for key in THREAD_KEYS}
            assert row["execution"]["cpus"] == cpus and row["execution"]["container"] == images["ks4"]
            assert row["execution"]["memory"] == ("512 MB" if cpus == 1 else "768 MB")
            assert row["execution"]["disk"] == ("128 MB" if cpus == 1 else "256 MB")
            expected_accelerator = {"request": 1, "limit": 1} if device == "cuda" else None
            assert row["execution"]["accelerator"] == expected_accelerator
            sentinels = [path / "sentinel.txt" for path in Path(row["layout"]).iterdir()
                         if path.is_dir() and (path / "sentinel.txt").is_file()]
            assert len(sentinels) == 1 and sentinels[0].read_text() == unit
            report = next(row for row in reports if (row["alias"], row["unit"]) == (alias, unit))
            assert report["recording"] == unit and report["execution"]["container"] == analysis
            report_cpus = 3 if alias == "alpha" else 2
            assert report["execution"]["cpus"] == report_cpus
            published = root / "published" / alias / "reports" / unit / "probe.json"
            output = json.loads(published.read_text())
            assert output["unit"] == unit and output["params"]["sorter"]["probe_marker"] == alias
            assert output["report_threads"] == {key: str(report_cpus) for key in THREAD_KEYS}
    assert job["unit"] == "job-probe" and job["duration"] == "7.5" and job["execution"]["container"] == images["base"]
    for alias in ("alpha", "beta"):
        published = root / "published" / alias
        assert {str(path.relative_to(published)) for path in published.rglob("*") if path.is_file()} == {
            "reports/slow/probe.json", "reports/fast/probe.json"}
    assert (Path(job["layout"]) / "ecephys_session/sentinel.txt").read_text() == "job-probe"
    assert json.loads((Path(job["results"]) / "job_0.json").read_text())["recording_name"] == "job-probe"
    with (root / "trace.tsv").open() as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    assert len(rows) == 9 and all(row["status"] == "COMPLETED" and row["exit"] == "0" for row in rows)
    for alias in ("ALPHA", "BETA"):
        completed = sorted((row for row in rows if row["name"].startswith(alias + ":spikesort_kilosort4")), key=lambda row: row["complete"])
        fast = next(item for item in sorting if item["alias"] == alias.lower() and item["unit"] == "fast")
        fast_task = Path(fast["results"]).resolve().parents[1]
        prefix, suffix = completed[0]["hash"].split("/")
        assert fast_task.parent.name == prefix and fast_task.name.startswith(suffix), (alias, completed)
    result = {"passed": True, "tasks": len(rows), "mode": args.mode, "legacy_runtime": bool(args.legacy_runtime),
              "scientific_capsules_stubbed": True, "cluster_work_submitted": False, "cuda_computation": False,
              "unit_keys_and_input_layouts_verified": True, "out_of_order_completion_verified": True,
              "independent_argv_resources_images_and_publication": True,
              "nrp_unqualified_resource_fallback_verified": True, "sorting": sorting, "reports": reports, "job": job}
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key not in {"sorting", "reports", "job"}}, indent=2))


if __name__ == "__main__":
    main()
