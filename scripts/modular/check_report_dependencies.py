#!/usr/bin/env python3
"""Run real reports with external recording dependencies and bounded task mounts.

The fixture has an empty real analyzer backed by two binary recordings in
same-named directories outside the source/work root. Every positive task reads
all recording frames before executing the unchanged report capsule. An omitted
dependency list must fail. Tests both fan-out and actual report publication/cache
policies; it performs no sorting, GPU or cluster work.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def fixture(root, dependencies):
    import numpy as np
    import spikeinterface as si
    from probeinterface import generate_linear_probe

    recordings = []
    for index, kind in enumerate(("raw", "prepared")):
        folder = dependencies / kind / "shared"
        folder.mkdir(parents=True)
        traces = ((np.arange(32000).reshape(8000, 4) + index * 11) % 1000 - 500).astype("int16")
        path = folder / "recording.raw"
        traces.tofile(path)
        recordings.append(si.BinaryRecordingExtractor(file_paths=path, sampling_frequency=30000,
                                                       num_channels=4, dtype="int16"))
    recording = si.concatenate_recordings(recordings)
    recording.set_channel_gains(1.0)
    recording.set_channel_offsets(0.0)
    probe = generate_linear_probe(num_elec=4, ypitch=20)
    probe.set_device_channel_indices(np.arange(4))
    recording = recording.set_probe(probe)
    empty = np.array([], dtype="int64")
    sorting = si.NumpySorting.from_samples_and_labels([empty], [empty], 30000, unit_ids=empty)
    analyzer = si.create_sorting_analyzer(sorting, recording, sparse=False, format="zarr",
                                          folder=root / "analyzer.zarr")
    analyzer.compute("random_spikes", method="all")
    analyzer.compute("waveforms", n_jobs=1)
    analyzer.compute("templates")
    analyzer.compute("noise_levels", chunk_size=1000)
    analyzer.compute("unit_locations")
    analyzer.compute("template_metrics")
    analyzer.compute("quality_metrics", metric_names=["num_spikes", "firing_rate", "presence_ratio"])
    expected = {"frames": 16000, "channels": 4,
                "sha256": hashlib.sha256(recording.get_traces().tobytes()).hexdigest()}
    (root / "code/reference.json").write_text(json.dumps(expected))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dependencies-root", type=Path, required=True)
    parser.add_argument("--legacy-runtime", type=Path)
    parser.add_argument("--fixture", action="store_true")
    args = parser.parse_args()
    root, dependencies = args.output.resolve(), args.dependencies_root.resolve()
    if args.fixture:
        fixture(root, dependencies)
        return
    assert root not in dependencies.parents and dependencies != root
    root.mkdir(parents=True, exist_ok=False)
    dependencies.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[2]
    image = json.loads((source / "containers/analysis/image.json").read_text())["local_image_id"]
    code = root / "code"
    shutil.copytree(source / "capsules/report_generation", code)
    # Preserve the owning capsule bytes; this test-only wrapper adds a complete
    # recording read and a dependency association receipt before invoking it.
    (code / "run_capsule.py").rename(code / "report_capsule.py")
    (code / "run_capsule.py").write_text('''import hashlib,json,runpy,sys
from pathlib import Path
import spikeinterface as si
a=sys.argv[1:]; analyzer=Path(a[a.index('--analyzer-dir')+1]); output=Path(a[a.index('--output-dir')+1])
recording=si.load_sorting_analyzer(analyzer).recording
actual={'frames':recording.get_num_samples(),'channels':recording.get_num_channels(),
        'sha256':hashlib.sha256(recording.get_traces().tobytes()).hexdigest()}
assert actual==json.loads((Path(__file__).parent/'reference.json').read_text()),actual
staged=sorted(Path('capsule/recording-dependencies').glob('input*/shared/recording.raw'))
assert len(staged)==2 and all(path.is_file() for path in staged)
actual['staged_dependencies']=[str(path) for path in staged]
(output/'recording-check.json').write_text(json.dumps(actual))
runpy.run_path(str(Path(__file__).parent/'report_capsule.py'),run_name='__main__')
''')
    command = ["docker", "run", "--rm", "--network", "none", "--user", "1000:1000",
               "--cpus", "1", "--memory", "2g", "-v", f"{source}:{source}:ro", "-v", f"{root}:{root}",
               "-v", f"{dependencies}:{dependencies}", "-e", "OPENBLAS_NUM_THREADS=1",
               image, "python", str(Path(__file__).resolve()), "--fixture", "--output", str(root),
               "--dependencies-root", str(dependencies)]
    with (root / "fixture.log").open("w") as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
    (root / "fixture-command.json").write_text(json.dumps(command, indent=2))
    executable, env = "nextflow", os.environ.copy()
    if args.legacy_runtime:
        cache = args.legacy_runtime
        executable = str(cache / "nextflow-23.08.0-edge-all")
        assert hashlib.sha256(Path(executable).read_bytes()).hexdigest() == "4f667c0ce1b939133baadd18678c83651e06f4bc676112af3b42d2c940ce5ac2"
        env.update(JAVA_HOME=str(cache / "jdk-17.0.16+8-jre"), NXF_HOME=str(cache / "home"),
                   NXF_DIST=str(cache / "framework"), NXF_PLUGINS_DIR=str(cache / "plugins"), NXF_VER="23.08.0-edge")
        env.pop("NXF_SYNTAX_PARSER", None)
    config = root / "nextflow.config"
    config.write_text("process.executor='local'\nprocess.cpus=1\nprocess.memory='2 GB'\n"
                      "executor.queueSize=2\nexecutor.pollInterval='1 sec'\ndocker.enabled=true\n"
                      "docker.runOptions='--user 1000:1000 -e HOME=/tmp -e MPLBACKEND=Agg'\n"
                      "trace.fields='task_id,hash,name,status,exit,container'\n")
    checks = {}

    def run(case, script, resume=False):
        tag = "resume" if resume else "initial"
        command = [executable, "-C", str(config), "run", str(script), "-ansi-log", "false",
                   "-work-dir", str(case / "work"), "-with-trace", str(case / (tag + "-trace.tsv"))]
        if resume:
            command.append("-resume")
        (case / (tag + "-command.json")).write_text(json.dumps(command, indent=2))
        with (case / (tag + ".log")).open("w") as log:
            result = subprocess.run(command, cwd=case, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=180)
        with (case / (tag + "-trace.tsv")).open() as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        return result, rows

    for policy in ("default", "true", "false", "missing"):
        case = root / policy
        case.mkdir()
        published = case / "published"
        settings = {"container": image, "results_path": str(published),
                    "publish_overwrite": None if policy in ("default", "missing") else policy == "true"}
        prefix = ("nextflow.enable.dsl=2\ninclude { meaCodeInput } from "
                  + json.dumps(str(source / "pipeline/code_identity"))
                  + "\ninclude { report_generation } from "
                  + json.dumps(str(source / "pipeline/modules/report_generation")) + "\nsettings=new groovy.json.JsonSlurper().parseText("
                  + json.dumps(json.dumps(settings)) + ")\nworkflow {\n")
        if policy == "missing":
            inputs = "inputs=channel.of(tuple('missing',file(" + json.dumps(str(root / "analyzer.zarr")) + "),[]))\n"
        else:
            # Exercise the direct caller's value broadcast to two reports.
            inputs = ("analyzers=channel.of(tuple('first',file(" + json.dumps(str(root / "analyzer.zarr"))
                      + ")),tuple('second',file(" + json.dumps(str(root / "analyzer.zarr")) + ")))\n"
                      + "deps=channel.of(file(" + json.dumps(str(dependencies / "raw/shared")) + "),file("
                      + json.dumps(str(dependencies / "prepared/shared")) + ")).collect()\n"
                      + "inputs=analyzers.combine(deps).map{fields -> tuple(fields[0],fields[1],fields.drop(2).sort{it.toString()})}\n")
        script = case / "main.nf"
        script.write_text(prefix + inputs + "report_generation(channel.value(settings),inputs,channel.value(meaCodeInput(file("
                          + json.dumps(str(code)) + "))))\n"
                          + "report_generation.out.unit_results.map{cfg,execution,id,results,data -> "
                          + "[id:id,results:results.toString(),data:data.toString()]}.collect().subscribe{rows -> "
                          + "file('emitted.json').text=groovy.json.JsonOutput.toJson(rows)}\n}\n")
        if policy != "missing":
            for unit in ("first", "second"):
                path = published / "reports" / unit
                path.mkdir(parents=True)
                (path / "report_summary.json").write_text('{"preexisting":true}')
        result, rows = run(case, script)
        if policy == "missing":
            assert result.returncode and len(rows) == 1 and rows[0]["status"] == "FAILED", rows
            assert "SortingAnalyzer could not load the recording" in (case / "initial.log").read_text()
            # Without dependencies the inferred mount must exclude their root.
            a, b = rows[0]["hash"].split("/")
            task = next((case / "work" / a).glob(b + "*"))
            docker_line = next(line for line in (task / ".command.run").read_text().splitlines() if "docker run" in line)
            assert str(dependencies) not in docker_line
            assert f"-v {root}:{root}" in docker_line, docker_line
            checks[policy] = {"exit": result.returncode, "tasks": rows, "mount": docker_line}
            continue
        assert result.returncode == 0 and len(rows) == 2 and all(row["status"] == "COMPLETED" for row in rows), rows
        emitted = json.loads((case / "emitted.json").read_text())
        assert {row["id"] for row in emitted} == {"first", "second"}
        for row in emitted:
            output = Path(row["results"]) / row["id"]
            assert json.loads((output / "report_summary.json").read_text())["status"] == "no_detected_units"
            assert len(json.loads((output / "recording-check.json").read_text())["staged_dependencies"]) == 2
            path = published / "reports" / row["id"] / "report_summary.json"
            actual = json.loads(path.read_text())
            assert actual == {"preexisting": True} if policy == "false" else actual["status"] == "no_detected_units"
            path.write_text('{"modified":true}')
        # Arrival order is execution detail. Reverse only the workflow's source
        # channel order; canonical dependency inputs must retain cached reports.
        old = "deps=channel.of(file(" + json.dumps(str(dependencies / "raw/shared")) + "),file(" + json.dumps(str(dependencies / "prepared/shared")) + ")).collect()"
        new = "deps=channel.of(file(" + json.dumps(str(dependencies / "prepared/shared")) + "),file(" + json.dumps(str(dependencies / "raw/shared")) + ")).collect()"
        assert old in script.read_text()
        script.write_text(script.read_text().replace(old, new))
        result, resumed = run(case, script, resume=True)
        assert result.returncode == 0 and len(resumed) == 2 and all(row["status"] == "CACHED" for row in resumed), resumed
        for unit in ("first", "second"):
            value = json.loads((published / "reports" / unit / "report_summary.json").read_text())
            assert value["status"] == "no_detected_units" if policy == "true" else value == {"modified": True}
        assert not list(published.rglob("recording.raw")) and not list(published.rglob("reference.json"))
        assert all(path.parts[len(published.parts)] == "reports" for path in published.rglob("*") if path.is_file())
        checks[policy] = {"initial": rows, "resume": resumed, "fanout": 2, "dependencies": 2}
    result = {"passed": True, "legacy_runtime": bool(args.legacy_runtime), "checks": checks,
              "real_report_capsule": True, "full_recording_read": True, "same_basename_dependencies": True,
              "no_gpu_or_cluster_work": True}
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "checks"}, indent=2))


if __name__ == "__main__":
    main()
