#!/usr/bin/env python3
"""Corrupt copied real outputs and require the scientific comparators to reject.

The original run stays read-only. Arrays mode uses the report image; NWB mode
uses the isolated pinned NWB-reader stack. These checks never execute sorting.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import zarr

from check_stage_science import Comparison, task, only, compare_products, compare_zarr, nwb
from science_provenance import canonical, sorter_log


def arrays(run, out):
    results = {}
    original = only(task(run, "postprocessing").glob("postprocessed_*.zarr"))
    changed = out / "changed-metric.zarr"
    shutil.copytree(original, changed)
    root = zarr.open(str(changed), "a")
    metric = root["extensions/quality_metrics/metrics/firing_rate"]
    before = float(metric[0])
    metric[0] = before + 1
    check = Comparison()
    compare_zarr(check, "analyzed", zarr.open(str(original), "r"), root)
    assert [row["field"] for row in check.differences] == ["analyzed/extensions/quality_metrics/metrics/firing_rate"]
    results["changed_metric"] = {"rejected": True, "before": before, "after": float(metric[0]),
                                  "differences": check.differences}

    original = task(run, "spikesort_kilosort4")
    missing = out / "missing-spikes"
    shutil.copytree(original, missing)
    spikes = only(missing.glob("spikesorted_*/spikes.npy"))
    spikes.rename(out / "quarantined-spikes.npy")
    check = Comparison()
    compare_products(check, "sorter", original, missing)
    assert [row["field"] for row in check.differences] == ["sorter.files"]
    results["missing_spike_payload"] = {"rejected": True, "differences": check.differences}

    # Exercise the actual retained sorter parameters/log through the same
    # normalizers used by full producer/analyzer comparisons.
    scratch = original.parent / "scratch"
    params = json.loads(only(scratch.rglob("spikeinterface_params.json")).read_text())
    modified = deepcopy(params)
    modified["sorter_params"]["Th_universal"] += 1
    check = Comparison()
    check.same("sorter.params", canonical(check, "original", params), canonical(check, "changed", modified))
    assert [row["field"] for row in check.differences] == ["sorter.params"]
    results["changed_sorter_parameter"] = {"rejected": True, "field": "sorter_params.Th_universal"}
    log = json.loads(only(scratch.rglob("spikeinterface_log.json")).read_text())
    modified = deepcopy(log)
    matches = [index for index, line in enumerate(modified["runtime_trace"]) if "N samples: 5400000" in line]
    assert len(matches) == 1
    index = matches[0]
    modified["runtime_trace"][index] = modified["runtime_trace"][index].replace("5400000", "5400001")
    check = Comparison()
    check.same("sorter.log", sorter_log(check, "original", log), sorter_log(check, "changed", modified))
    assert [row["field"] for row in check.differences] == ["sorter.log"]
    results["changed_sorter_scientific_count"] = {"rejected": True, "field": "N samples"}
    modified = deepcopy(log)
    modified["error"] = True
    try:
        sorter_log(Comparison(), "failed", modified)
    except AssertionError as error:
        results["failed_sorter_terminal_evidence"] = {"rejected": True, "error": str(error)}
    else:
        raise AssertionError("Sorter failure was normalized away")
    return results


def authored_nwb(run, source_nwb, out):
    view = out / "changed-authored-run"
    (view / "evidence").mkdir(parents=True)
    shutil.copy2(run / "evidence/trace.tsv", view / "evidence/trace.tsv")
    # Keep real task identities. Only the selected NWB result is copied/changed.
    import csv
    for row in csv.DictReader((run / "evidence/trace.tsv").open(), delimiter="\t"):
        first, rest = row["hash"].split("/")
        original = only((run / "work" / first).glob(rest + "*"))
        parent = view / "work" / first
        parent.mkdir(parents=True, exist_ok=True)
        destination = parent / original.name
        if row["name"].split(" (")[0].split(":")[-1] == "nwb_units":
            (destination / "capsule").mkdir(parents=True)
            shutil.copytree(original / "capsule/results", destination / "capsule/results")
        else:
            destination.symlink_to(original)
    path = only(task(view, "nwb_units").glob("*.nwb"))
    root = zarr.open(str(path), "a")
    before = root["identifier"][:].tolist()
    root["identifier"][...] = "changed-authored-identifier"
    check = Comparison()
    nwb(SimpleNamespace(direct=run, grouped=view, source_nwb=source_nwb), check)
    fields = [row["field"] for row in check.differences]
    assert "nwb_units.authored_header.1" in fields and "nwb_units/identifier" in fields, fields
    assert set(fields) == {"nwb_units.authored_header.1", "nwb_units/identifier"}, fields
    return {"changed_authored_nwb_identifier": {"rejected": True, "before": before,
                                               "after": root["identifier"][:].tolist(),
                                               "differences": check.differences}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=("arrays", "nwb"), required=True)
    parser.add_argument("--source-nwb", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    results = arrays(args.run, args.output) if args.phase == "arrays" else authored_nwb(args.run, args.source_nwb, args.output)
    report = {"passed": True, "phase": args.phase, "original_run": str(args.run), "cases": results,
              "source_outputs_changed": False, "scientific_computation_executed": False}
    (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": True, "phase": args.phase, "cases": list(results)}, indent=2))


if __name__ == "__main__":
    main()
