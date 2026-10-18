#!/usr/bin/env python3
"""Replay old/fixed collectors across real and symlinked reference bases.

Run in the pinned base image with original task trees read-only. This targets the
single 15-unit reference fixture; it replays collection, not sorting/analysis.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import numpy as np
import spikeinterface as si
import zarr

from check_stage_science import Comparison, compare_recordings, compare_sortings, compare_zarr

SOURCE = Path(__file__).resolve().parents[2]


def only(paths):
    values = list(paths)
    assert len(values) == 1, values
    return values[0]


def raw_base(task):
    line = next(line for line in (task / ".command.sh").read_text().splitlines()
                if line.startswith("./run --pipeline-data-path "))
    argv = shlex.split(line)
    return Path(argv[argv.index("--pipeline-data-path") + 1])


def replay(args, name, code, task, aliased_publication=False):
    root = args.output / name
    (root / "capsule").mkdir(parents=True)
    shutil.copytree(code, root / "capsule/code")
    (root / "capsule/data").symlink_to(task / "capsule/data")
    (root / "capsule/results").mkdir()
    (root / "capsule/scratch").mkdir()
    if aliased_publication:
        physical = root / "future/nested/storage"
        physical.mkdir(parents=True)
        (root / "future-alias").symlink_to(physical)
        publication = root / "future-alias/unpublished"
    else:
        publication = root / "unpublished"
    assert not publication.exists()
    base = raw_base(task)
    command = [sys.executable, "run_capsule.py", "--pipeline-data-path", str(base),
               "--pipeline-results-path", str(publication)]
    (root / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    with (root / "capsule.log").open("w") as log:
        result = subprocess.run(command, cwd=root / "capsule/code", stdout=log,
                                stderr=subprocess.STDOUT, timeout=180)
    assert result.returncode == 0, (name, (root / "capsule.log").read_text()[-3000:])
    assert not publication.exists(), "Reference base must not require publication-directory creation"
    preprocessed = only((root / "capsule/results/preprocessed").glob("*.json"))
    analyzer_path = only((root / "capsule/results/postprocessed").glob("*.zarr"))
    outcome = {"exit": result.returncode, "raw_base": str(base), "publication": str(publication),
               "publication_still_absent": True, "result_directory": str(root / "capsule/results"),
               "preprocessed_path": str(preprocessed), "analyzer_path": str(analyzer_path)}
    loaded = {}
    for kind in ("preprocessed", "analyzer"):
        try:
            if kind == "preprocessed":
                value = si.load(preprocessed, base_folder=base)
            else:
                rec = si.load(zarr.open(str(analyzer_path), mode="r")["recording"][0],
                              base_folder=publication / "postprocessed" / analyzer_path.name)
                value = si.SortingAnalyzer.load_from_zarr(analyzer_path, recording=rec)
            recording = value if kind == "preprocessed" else value.recording
            assert recording is not None
            traces = recording.get_traces(start_frame=0, end_frame=128)
            assert traces.shape == (128, 32) and np.isfinite(traces).all()
            loaded[kind] = value
            outcome[kind] = {"readable": True}
        except (RuntimeError, FileNotFoundError) as exc:
            outcome[kind] = {"readable": False, "error": type(exc).__name__ + ": " + str(exc)}
    (root / "result.json").write_text(json.dumps(outcome, indent=2) + "\n")
    return outcome, loaded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("direct-task", "relocated-task", "old-code", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cases, values = {}, {}
    for layout, task, aliased in (("direct", args.direct_task, False),
                                  ("relocated", args.relocated_task, False),
                                  ("future-alias", args.direct_task, True)):
        for version, code in (("old", args.old_code), ("fixed", SOURCE / "capsules/results_collector/code")):
            name = layout + "-" + version
            cases[name], values[name] = replay(args, name, code, task, aliased)
            if version == "fixed":
                assert cases[name]["preprocessed"]["readable"] and cases[name]["analyzer"]["readable"], cases[name]
            print(name, {key: cases[name][key]["readable"] for key in ("preprocessed", "analyzer")}, flush=True)
    assert cases["direct-old"]["preprocessed"]["readable"] and cases["direct-old"]["analyzer"]["readable"]
    assert not cases["relocated-old"]["preprocessed"]["readable"]
    assert not cases["future-alias-old"]["analyzer"]["readable"]
    check = Comparison()
    baseline = values["direct-fixed"]
    for name in ("direct-old", "relocated-fixed", "future-alias-fixed"):
        pair = values[name]
        compare_recordings(check, name + ".preprocessed", baseline["preprocessed"], pair["preprocessed"])
        compare_recordings(check, name + ".analyzer.recording", baseline["analyzer"].recording, pair["analyzer"].recording)
        compare_sortings(check, name + ".sorting", baseline["analyzer"].sorting, pair["analyzer"].sorting)
        compare_zarr(check, name + ".analyzer", zarr.open(cases["direct-fixed"]["analyzer_path"], mode="r"),
                     zarr.open(cases[name]["analyzer_path"], mode="r"))
    for name in ("direct-fixed", "relocated-fixed", "future-alias-fixed"):
        analyzer = values[name]["analyzer"]
        assert analyzer.get_num_units() == 15 and len(analyzer.sorting.to_spike_vector()) == 40440
        assert len(analyzer.sorting.get_property("default_qc")) == 15
    # A real dependency loss stays a loader error after the base repair.
    from spikeinterface.core.core_tools import extractor_dict_iterator, set_value_in_extractor_dict
    wrong = deepcopy(json.loads(Path(cases["relocated-fixed"]["preprocessed_path"]).read_text()))
    changed = 0
    for item in list(extractor_dict_iterator(wrong)):
        if item.name in ("file_path", "folder_path"):
            set_value_in_extractor_dict(wrong, item.access_path, "missing-scientific-recording")
            changed += 1
    assert changed
    try:
        si.load(wrong, base_folder=Path(cases["relocated-fixed"]["raw_base"]))
    except (RuntimeError, FileNotFoundError):
        missing_rejected = True
    else:
        raise AssertionError("Missing recording dependency accepted")
    result = {"passed": not check.differences, "actual_capsule_replays": 6,
              "scientific_recomputation": False, "cases": cases, "checked": check.checked,
              "differences": check.differences, "numeric_tolerance": 0,
              "execution_fields": sorted(set(check.execution_fields)), "missing_dependency_rejected": missing_rejected}
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    assert result["passed"], [item["field"] for item in check.differences]
    print(json.dumps({"passed": True, "checks": len(check.checked), "capsule_replays": 6}, indent=2))


if __name__ == "__main__":
    main()
