#!/usr/bin/env python3
"""Exercise parameter/pin changes across real Nextflow cache/resume cycles.

Uses the actual settings parser and process modules with tiny argv-recording
capsules. Run in a finite driver Job or local container; no science is performed.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", default="nextflow")
    parser.add_argument("--label", default="current")
    args = parser.parse_args()
    root = Path(os.environ["TEST_ROOT"])
    source = Path(os.environ["SOURCE_DIR"])
    folder = root / "probes" / f"parameter-cache-{args.label}"
    folder.mkdir(parents=True, exist_ok=True)
    # Run the actual imported process modules with tiny argv-recording capsules.
    # This checks their real value/path inputs and cache keys, not a copied shell task.
    code = folder / "probe-code"
    code.mkdir(exist_ok=True)
    (code / "run").write_text("""#!/usr/bin/env bash
exec python3 - "$@" <<'PYTHON'
import json, sys
from pathlib import Path
out = Path('../results')
out.mkdir(parents=True, exist_ok=True)
(out / 'arguments.json').write_text(json.dumps(sys.argv[1:]))
PYTHON
""")
    (root / "data").mkdir(exist_ok=True)
    prepared = folder / "prepared"
    prepared.mkdir(exist_ok=True)
    dispatch = folder / "job.json"
    dispatch.write_text('{}')
    script = folder / "main.nf"
    script.write_text("""nextflow.enable.dsl = 2
include { loadLaunchSettings; processOptions } from '""" + str(source / "pipeline/settings") + """'
include { spikesort_kilosort4 as SORTER_PARAMETERS } from '""" + str(source / "pipeline/modules/spikesort_kilosort4") + """'
include { preprocessing as PREPROCESSING_PARAMETERS } from '""" + str(source / "pipeline/modules/preprocessing") + """'
params.params_file = null
params.torch_device = null
params.results_path = null
params.publish_overwrite = null
params.git_repo_prefix = 'https://github.com/AllenNeuralDynamics/aind-'
settings = loadLaunchSettings(params, baseDir)
workflow {
    SORTER_PARAMETERS(channel.value(processOptions('spikesort_kilosort4', settings)),
        file('""" + str(code) + """'), channel.value(1), file('""" + str(prepared) + """'))
    PREPROCESSING_PARAMETERS(channel.value(processOptions('preprocessing', settings)),
        file('""" + str(code) + """'), channel.value(1), file('""" + str(root / 'data') + """'), file('""" + str(dispatch) + """'))
}
""")
    for name in ("capsule_versions.env", "kilosort4_defaults.json"):
        shutil.copy2(source / "pipeline" / name, folder / name)
    config = folder / "local.config"
    config.write_text("process.executor = 'local'\nprocess.cpus = 1\nparams.executor = 'k8s'\n")
    params = json.loads((source / "scripts/params_no_motion.json").read_text())
    params["preprocessing"] = {"motion": "skip"}
    params_file = folder / "params.json"
    outcomes = {}

    def run(label, expected, resume=True):
        params_file.write_text(json.dumps(params, indent=2))
        trace = folder / f"{label}.tsv"
        command = [args.executable, "-log", str(folder / f"{label}.nextflow.log"),
                   "-C", str(config), "run", str(script), "--ecephys_path", str(root / "data"),
                   "--params_file", str(params_file), "--torch_device", "cuda",
                   "-with-trace", str(trace)]
        if resume:
            command.append("-resume")
        with (folder / f"{label}.log").open("w") as log:
            subprocess.run(command, cwd=folder, stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=300)
        with trace.open() as file:
            rows = list(csv.DictReader(file, delimiter="\t"))
        statuses = {row["name"].split(" (", 1)[0]: row["status"] for row in rows}
        outcomes[label] = statuses
        print(label, statuses, flush=True)
        assert statuses == expected, (label, statuses, expected)
        for row in rows:
            prefix, suffix = row["hash"].split("/")
            work = next((folder / "work" / prefix).glob(suffix + "*"))
            arguments = json.loads((work / "capsule/results/arguments.json").read_text())
            actual = json.loads(arguments[arguments.index("--params") + 1])
            if row["name"].split(" (", 1)[0] == "SORTER_PARAMETERS":
                desired = json.loads(json.dumps(params["spikesorting"]["kilosort4"]))
                desired["sorter"]["torch_device"] = "cuda"
                assert actual == desired, (label, "Sorter settings were stale")
            else:
                assert actual == params["preprocessing"], (label, "Preprocessing settings were stale")

    complete = {"SORTER_PARAMETERS": "COMPLETED", "PREPROCESSING_PARAMETERS": "COMPLETED"}
    cached = {name: "CACHED" for name in complete}
    run("initial", complete, resume=False)
    run("unchanged", cached)
    params["spikesorting"]["kilosort4"]["sorter"]["templates_from_data"] = False
    run("sorter-change", dict(cached, SORTER_PARAMETERS="COMPLETED"))
    run("sorter-unchanged", cached)
    params["preprocessing"]["motion"] = "compute"
    run("preprocessing-change", dict(cached, PREPROCESSING_PARAMETERS="COMPLETED"))
    pins = folder / "capsule_versions.env"
    lines = pins.read_text().splitlines()
    pins.write_text("\n".join("SPIKESORT_KS4=" + "0" * 40 if line.startswith("SPIKESORT_KS4=")
                              else line for line in lines) + "\n")
    run("capsule-pin-change", dict(cached, SORTER_PARAMETERS="COMPLETED"))
    run("pin-unchanged", cached)
    result = {"passed": True, "source": str(source), "checks": outcomes,
              "actual_process_modules": True, "scientific_capsules_stubbed": True}
    (folder / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
