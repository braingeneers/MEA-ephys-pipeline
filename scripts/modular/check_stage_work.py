#!/usr/bin/env python3
"""Check required-work guards using retained real capsule processing records.

These are metadata contract controls, not generated sorting or science evidence.
Run in the pinned base image; keep the supplied records and output outside Git.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline/helpers"))
import portable_bundle as portable
import stage_bundle as stages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preprocessing-record", type=Path, required=True)
    parser.add_argument("--sorting-record", type=Path, required=True)
    parser.add_argument("--zero-sorter-log", type=Path, required=True)
    parser.add_argument("--recording-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    for role, source in [("preprocessing", args.preprocessing_record), ("spikesorting", args.sorting_record)]:
        original_bytes = source.read_bytes()
        original = json.loads(original_bytes)
        original_sha = hashlib.sha256(original_bytes).hexdigest()
        controls = [("valid", None, True), ("missing", None, False),
                    ("invalid-json", "{", False), ("non-object", [], False)]
        for name, key, value in [
            ("wrong-owner", "process_type", "Unrelated process"),
            ("wrong-stage", "stage", "Unfinished"),
            ("missing-code", "code", None),
            ("non-object-output", "output_parameters", []),
            ("missing-start", "start_date_time", None),
            ("invalid-end", "end_date_time", "unfinished"),
            ("reversed-clock", "end_date_time", "1900-01-01T00:00:00Z"),
        ]:
            changed = deepcopy(original)
            changed[key] = value
            controls.append((name, changed, False))
        changed = deepcopy(original)
        changed["code"]["parameters"]["recording_name" if role == "preprocessing" else "sorter_name"] = "different-owner"
        controls.append(("wrong-recording-or-sorter", changed, False))
        changed = deepcopy(original)
        changed["code"]["parameters"] = []
        controls.append(("non-object-parameters", changed, False))
        for name, changed, expected in controls:
            case_id = role + "-" + name
            data = args.output / case_id
            data.mkdir()
            path = data / f"data_process_{role}_{args.recording_id}.json"
            if name == "valid":
                path.write_bytes(original_bytes)
            elif name != "missing":
                path.write_text(changed if name == "invalid-json" else json.dumps(changed))
            try:
                result = stages.completion_record(data, args.recording_id, role)
            except portable.BundleError as error:
                accepted, message = False, str(error)
                expected_message = ("Missing" if name == "missing" else "Malformed") + f" {role} completion record for {args.recording_id}"
                assert message == expected_message, (case_id, message)
            else:
                accepted, message = True, None
                assert portable.encoded(result) == portable.encoded(original)
            assert accepted is expected, (case_id, accepted, expected)
            results.append(dict(case_id=case_id, passed=True, accepted=accepted, error=message,
                                original_sha256=original_sha))
        assert source.read_bytes() == original_bytes
    original_bytes = args.zero_sorter_log.read_bytes()
    original = json.loads(original_bytes)
    classify = stages.classifier(Path(__file__).resolve().parents[2] / "capsules/kilosort4/code/outcomes.py")
    log_controls = [("owned-zero", deepcopy(original), True)]
    for name, key, value in [
        ("wrong-sorter", "sorter_name", "kilosort2"),
        ("non-boolean-error", "error", 1),
        ("missing-terminal-error", "error_trace", []),
        ("unknown-terminal-error", "error_trace", ["RuntimeError: CUDA failure"]),
    ]:
        changed = deepcopy(original)
        changed[key] = value
        log_controls.append((name, changed, False))
    for name, remove in [
        ("incomplete-detection", lambda line: re.search(r"\b0 spikes extracted in ", line)),
        ("missing-empty-shape", lambda line: "st0 shape: (0, 6)" in line),
    ]:
        changed = deepcopy(original)
        changed["runtime_trace"] = [line for line in original["runtime_trace"] if not remove(line)]
        log_controls.append((name, changed, False))
    for name, value, expected in log_controls:
        accepted = classify(value)
        assert accepted is expected, (name, accepted, expected)
        path = args.output / ("sorter-log-" + name + ".json")
        path.write_text(json.dumps(value, indent=2) + "\n")
        results.append(dict(case_id="sorter-log-" + name, passed=True, recognized=accepted,
                            original_sha256=hashlib.sha256(original_bytes).hexdigest()))
    assert args.zero_sorter_log.read_bytes() == original_bytes
    report = dict(passed=True, tests=len(results), results=results,
                  scope="Real original DataProcess metadata and retained malformed/missing controls; no scientific producer or sorting result generated")
    (args.output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(passed=True, tests=len(results))))


if __name__ == "__main__":
    main()
