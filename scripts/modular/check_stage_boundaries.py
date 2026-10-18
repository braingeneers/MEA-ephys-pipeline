#!/usr/bin/env python3
"""Exercise stage policy/assembly on a retained real direct run, without sorting."""
import argparse
import csv
import importlib.util
import json
from pathlib import Path
import sys
import time


SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / "pipeline/helpers"))
import portable_bundle as portable
import stage_bundle as stages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with (args.run / "evidence/trace.tsv").open() as stream:
        rows = {row["name"].split(" (")[0]: row for row in csv.DictReader(stream, delimiter="\t")}

    def task(name):
        prefix, suffix = rows[name]["hash"].split("/")
        matches = list((args.run / "work" / prefix).glob(suffix + "*"))
        assert len(matches) == 1
        return matches[0]

    outcomes = stages.classifier(SOURCE / "capsules/kilosort4/code/outcomes.py")
    previous = None
    results = {}
    start = time.monotonic()
    for phase, producers in (("dispatch", ["job_dispatch"]), ("prepared", ["preprocessing"]),
                             ("sorted", ["spikesort_kilosort4"]),
                             ("analyzed", ["postprocessing", "curation", "visualization", "results_collector"]),
                             ("reported", ["report_generation", "burst_detection"]), ("exported", ["nwb_units"])):
        paths = []
        request = {"schema_version": 1, "unit_id": "reference", "unit_meta": {"unit_id": "reference", "preserve_me": "inventory"},
                   "phase": phase, "context": {"workflow_version": "0.2.0", "output_uri": str(args.output), "step_id": phase},
                   "pieces": [], "processes": [{"name": name, "retained_real_task": rows[name]["hash"]} for name in producers]}
        if previous:
            request["previous"] = len(paths)
            paths.append(previous)
        for name in producers:
            piece = {"results": len(paths)}
            paths.append(task(name) / "capsule/results")
            piece["inputs"] = len(paths)
            paths.append(task(name) / "capsule/data")
            if name == "results_collector":
                piece.update(role="collector", destination="data/collected", publication=str(args.run / 'results'))
            elif name == "report_generation":
                piece["destination"] = "data/reports"
            elif name == "burst_detection":
                piece["destination"] = "data/bursts"
            elif name == "nwb_units":
                piece["destination"] = "data/derived-nwb"
            request["pieces"].append(piece)
        if phase == "dispatch":
            request["duration"] = len(paths)
            paths.append(task("job_dispatch") / "max_duration.txt")
            request["source"] = len(paths)
            paths.append(args.raw)
        request["input_count"] = len(paths)
        target = args.output / phase
        record = stages.build(request, paths, target, outcomes)
        stages.verify(target, record["stage"], "reference", outcomes)
        assert record["metadata"]["unit_meta"]["preserve_me"] == "inventory"
        results[phase] = {"bytes": record["bytes"], "recordings": record["recording_ids"], "outcomes": record["metadata"]["outcomes"]}
        previous = target
        print(phase, "PASS", flush=True)
    record = stages.read_record(previous)
    assert sum(item["analyzed_units"] for item in record["metadata"]["outcomes"].values()) == 15
    assert sum(item["curated_spikes"] for item in record["metadata"]["outcomes"].values()) == 40440
    invalid = dict(record, metadata=dict(record["metadata"], completed_stages=["exported"]))
    try:
        stages.scientific_policy(previous, invalid, outcomes)
    except portable.BundleError as error:
        assert "history" in str(error)
    else:
        raise AssertionError("Final stage accepted missing predecessor history")
    result = {"passed": True, "retained_real_artifacts": True, "scientific_recomputation": False,
              "stages": results, "missing_history_rejected": True, "duration_seconds": time.monotonic() - start}
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
