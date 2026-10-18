#!/usr/bin/env python3
"""Run real standalone stages with copied inputs and audited Docker task mounts.

The stdlib coordinator runs beside Docker; each Nextflow driver receives only its
own stage root. Every actual task request passes the copied script as a Docker
wrapper, and an in-container preflight proves original paths are unavailable.
This is reference validation tooling, never a production launch adapter.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time


DRIVER = "braingeneers/workflows-nextflow-runner@sha256:0207c6c32f848e40987b04c6e2ee78f85c43aad2a694ac861ea64a998c2ae900"
REPORT = "sha256:a372fb3a91f87543c4cc93a41ecb160056832b456dbfffcb0ceded675f54acaf"
STAGES = [("preprocessing", "prepared", 4), ("sorting", "sorted", 3),
          ("analysis", "analyzed", 6), ("reporting", "reported", 4), ("export", "exported", 4)]
THREADS = ("CO_CPUS", "N_JOBS_EXT", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS")


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def under(path, root):
    return os.path.commonpath([str(Path(path).resolve()), str(root)]) == str(root)


def inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Physical isolation requires real copied files: " + str(path))
        if path.is_file():
            result[str(path.relative_to(root))] = {"bytes": path.stat().st_size, "sha256": sha(path)}
    return result


def copied_tree(source, target):
    before = inventory(source)
    shutil.copytree(source, target)
    assert inventory(target) == before, "Input copy verification failed"
    return {"source": str(source), "destination": str(target), "files": before}


def docker_guard():
    policy = read(os.environ["MEA_ISOLATION_POLICY"])
    root = Path(policy["root"]).resolve()
    args = sys.argv[1:]
    if args and args[0] == "run":
        mounts, workdir = [], None
        denied = []
        for index, arg in enumerate(args):
            value = None
            if arg in ("-v", "--volume", "--mount", "-w", "--workdir"):
                value = args[index + 1]
            elif arg.startswith(("--volume=", "--mount=", "--workdir=")):
                value = arg.split("=", 1)[1]
            if value is None:
                continue
            if arg.startswith(("--workdir", "-w")):
                workdir = value
                if not under(value, root):
                    denied.append("Work directory is outside the selected stage root")
                continue
            if arg.startswith("--mount"):
                options = dict(part.split("=", 1) for part in value.split(",") if "=" in part)
                source = options.get("source", options.get("src", ""))
                if options.get("type", "bind") != "bind":
                    denied.append("Undeclared non-bind mount")
            else:
                source = value.split(":", 1)[0]
            resolved = str(Path(source).resolve()) if source else ""
            mounts.append({"requested": value, "source": source, "resolved": resolved})
            if not source.startswith("/") or not under(source, root):
                denied.append("Mount is outside the selected stage root: " + source)
        if not mounts:
            denied.append("A task must declare its bounded mounts")
        if any(arg.startswith(("--privileged", "--volumes-from", "--device", "--pid=host")) for arg in args):
            denied.append("Task requested access beyond the bounded filesystem")
        images = [arg for arg in args if arg in policy["images"]]
        if len(images) != 1:
            denied.append("Task image is not exactly one declared pinned image")
        record = {"argv": args, "mounts": mounts, "workdir": workdir,
                  "image": images[0] if len(images) == 1 else None, "denied": denied,
                  "at_utc": datetime.now(timezone.utc).isoformat()}
        with Path(policy["guard_receipts"]).open("a") as stream:
            stream.write(json.dumps(record) + "\n")
        if denied:
            print("Isolated task request rejected: " + "; ".join(denied), file=sys.stderr)
            raise SystemExit(79)
    os.execv("/usr/local/bin/docker-real", ["docker", *args])


def preflight(policy_path):
    policy = read(policy_path)
    root = Path(os.environ["MEA_ISOLATION_TASK_ROOT"])
    unavailable = {value: not Path(value).exists() for value in policy["forbidden"]}
    assert all(unavailable.values()), unavailable
    threads = {key: os.environ.get(key) for key in THREADS}
    assert set(threads.values()) == {"2"}, threads
    record = {"task_root": str(root), "unavailable": unavailable, "threads": threads,
              "mountinfo": Path("/proc/self/mountinfo").read_text()}
    with (root / "isolation-visibility.jsonl").open("a") as stream:
        stream.write(json.dumps(record) + "\n")


def run_recorded(command, cwd, log, timeout=3600):
    start = time.monotonic()
    with log.open("w") as stream:
        result = subprocess.run(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
    return {"exit": result.returncode, "duration_seconds": time.monotonic() - start,
            "ended_at_utc": datetime.now(timezone.utc).isoformat()}


def stage(args, stage_name, kind, expected_tasks, selected):
    root = args.output / stage_name
    root.mkdir(parents=True, exist_ok=False)
    tools = root / "validation"
    tools.mkdir()
    script = tools / "check_stage_isolation.py"
    shutil.copy2(Path(__file__).resolve(), script)
    script.chmod(0o755)
    source = root / "source"
    source.mkdir()
    snapshot = args.source / "source-manifest.json"
    if snapshot.is_file():
        tracked = list(read(snapshot)["files"])
    else:
        tracked = subprocess.check_output(["git", "-C", str(args.source), "ls-files", "-z"]).decode().strip("\0").split("\0")
    closure = [name for name in tracked if name.startswith(("pipeline/", "capsules/")) or name == "scripts/nrp/images.json"]
    assert closure
    for name in closure:
        origin, target = args.source / name, source / name
        assert origin.is_file() and not origin.is_symlink(), name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, target)
    write(root / "runtime-closure.json", {"paths": closure, "files": inventory(source)})
    (root / "input").mkdir()
    input_path = root / "input/selected"
    write(root / "input-copy.json", copied_tree(selected, input_path))
    shutil.copy2(args.params, root / "input/controls.json")
    write(root / "input-inventory.json", inventory(root / "input"))
    images = read(source / "scripts/nrp/images.json")
    policy = {"root": str(root), "forbidden": [str(path) for path in args.forbidden],
              "images": [images["base"], images["ks4"], images["nwb"], REPORT],
              "guard_receipts": str(root / "docker-requests.jsonl")}
    write(tools / "policy.json", policy)
    (tools / "preflight.sh").write_text(
        'export MEA_ISOLATION_TASK_ROOT="${MEA_ISOLATION_TASK_ROOT:-$PWD}"\n'
        + "python " + shlex.quote(str(script)) + " preflight " + shlex.quote(str(tools / "policy.json")) + "\n")
    # Record process-final worker/library settings without importing numerical
    # libraries ahead of the capsule's own initialization (notably BLAS=1).
    (tools / "sitecustomize.py").write_text(
        'import atexit,json,os,pathlib,sys\n'
        'if pathlib.Path(sys.argv[0]).name == "run_capsule.py":\n'
        ' def capture():\n'
        '  from threadpoolctl import threadpool_info\n'
        '  si=sys.modules.get("spikeinterface")\n'
        '  p=pathlib.Path(os.environ["MEA_ISOLATION_TASK_ROOT"])/"execution-inprocess.json"\n'
        '  p.write_text(json.dumps({"argv":sys.argv,"environment":{k:os.environ.get(k) for k in '
        + repr(THREADS) + '},"job_kwargs":si.get_global_job_kwargs() if si is not None else None,'
        '"threadpools":threadpool_info()},indent=2)+"\\n")\n'
        ' atexit.register(capture)\n')
    execution = root / "execution"
    execution.mkdir()
    options = ["--user", "1000:1000", "-e", "HOME=/tmp", "-e", "MPLBACKEND=Agg",
               "-v", str(source) + ":" + str(source) + ":ro",
               "-v", str(root / "input") + ":" + str(root / "input") + ":ro",
               "-v", str(tools) + ":" + str(tools) + ":ro",
               "-e", "BASH_ENV=" + str(tools / "preflight.sh"), "-e", "PYTHONPATH=" + str(tools)]
    groovy = lambda text: "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"
    (execution / "probe.config").write_text("executor.queueSize=4\nexecutor.pollInterval='1 sec'\n"
                                          + "docker.runOptions=" + groovy(shlex.join(options)) + "\n")
    command = ["nextflow", "-log", str(execution / "nextflow.log"), "-C",
               str(source / "pipeline/nextflow_stage_docker.config") + "," + str(execution / "probe.config"),
               "run", str(source / "pipeline/main_stages.nf"), "--stage", stage_name,
               "--unit_id", "reference", "--ecephys_path" if stage_name == "preprocessing" else "--input_bundle",
               str(input_path), "--results_path", str(execution / "results"), "--params_file", str(root / "input/controls.json"),
               "--torch_device", "cpu", "--n_jobs", "2", "--report_container_image", REPORT,
               "-ansi-log", "false", "-work-dir", str(execution / "work"), "-with-trace", str(execution / "trace.tsv")]
    write(execution / "nextflow-command.json", command)
    driver = ["docker", "run", "--rm", "--name", "mea-isolated-" + stage_name + "-" + str(os.getpid()),
              "--group-add", "999", "-v", "/var/run/docker.sock:/var/run/docker.sock",
              "-v", "/usr/bin/docker:/usr/local/bin/docker-real:ro",
              "-v", str(script) + ":/usr/local/bin/docker:ro", "-v", str(root) + ":" + str(root),
              "-v", str(source) + ":" + str(source) + ":ro",
              "-v", str(root / "input") + ":" + str(root / "input") + ":ro",
              "-e", "MEA_ISOLATION_POLICY=" + str(tools / "policy.json"), "-e", "NXF_SYNTAX_PARSER=v1",
              "-w", str(execution), "--entrypoint", "/bin/bash", DRIVER, "-c", shlex.join(command)]
    write(execution / "driver-command.json", driver)
    # These three real wrapper invocations must be rejected before Docker can
    # expose a broader ancestor, an original producer, or a symlink escape.
    probe = driver[:-2] + ["-c"]
    target = args.forbidden[0]
    escape = tools / "escape"
    escape.symlink_to(target)
    denied = []
    for candidate in (root.parent, target, escape):
        cmd = ["docker", "run", "--rm", "-v", str(candidate) + ":/probe", policy["images"][0], "true"]
        record = run_recorded(probe + [shlex.join(cmd)], execution,
                              execution / ("denial-" + str(len(denied)) + ".log"))
        assert record["exit"] == 79, record
        denied.append(record)
    escape.rename(args.output / (stage_name + "-denied-symlink"))
    write(execution / "denial-probes.json", denied)
    result = run_recorded(driver, execution, execution / "driver.log")
    write(execution / "exit.json", result)
    assert result["exit"] == 0, (stage_name, result, execution / "driver.log")
    rows = list(csv.DictReader((execution / "trace.tsv").open(), delimiter="\t"))
    assert len(rows) == expected_tasks and all(row["status"] == "COMPLETED" and row["exit"] == "0" for row in rows)
    requests = [json.loads(line) for line in (root / "docker-requests.jsonl").read_text().splitlines()]
    accepted = [item for item in requests if not item["denied"]]
    assert len(accepted) == expected_tasks and len(requests) == expected_tasks + 3
    for row in rows:
        first, rest = row["hash"].split("/")
        matches = list((execution / "work" / first).glob(rest + "*"))
        assert len(matches) == 1
        task = matches[0]
        visibility = [json.loads(line) for line in (task / "isolation-visibility.jsonl").read_text().splitlines()]
        assert visibility and all(all(item["unavailable"].values()) for item in visibility)
        assert any(item["workdir"] == str(task) and item["image"] == row["container"] for item in accepted)
    assert inventory(root / "input") == read(root / "input-inventory.json"), "Scientific stage changed its copied input"
    output = execution / "results" / stage_name / "bundles" / ("reference." + kind)
    record = read(output / "bundle.json")
    assert record["stage"] == kind and record["unit_id"] == "reference"
    result.update(passed=True, stage=stage_name, tasks=len(rows), bundle=str(output),
                  mount_requests=len(accepted), denied_mount_probes=3, input_unchanged=True)
    write(execution / "result.json", result)
    print(json.dumps(result), flush=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--params", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--forbid", type=Path, action="append", required=True)
    args = parser.parse_args()
    for name in ("source", "session", "params", "output"):
        setattr(args, name, getattr(args, name).resolve())
    args.forbidden = [p.resolve() for p in args.forbid]
    args.output.mkdir(parents=True, exist_ok=False)
    assert all(not under(args.output, forbidden) for forbidden in args.forbidden)
    selected = args.session
    for name, kind, tasks in STAGES:
        selected = stage(args, name, kind, tasks, selected)
    write(args.output / "result.json", {"passed": True, "stages": [s[0] for s in STAGES],
                                         "tasks": 21, "final_bundle": str(selected), "scientific_capsules_stubbed": False})


if __name__ == "__main__":
    if Path(sys.argv[0]).name == "docker":
        docker_guard()
    elif len(sys.argv) > 1 and sys.argv[1] == "preflight":
        preflight(sys.argv[2])
    else:
        main()
