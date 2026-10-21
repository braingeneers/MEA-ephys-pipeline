#!/usr/bin/env python3
"""Check report train bindings and typed burst receipts without running capsules."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys

import numpy as np
import spikeinterface as si
from threadpoolctl import threadpool_info

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / "pipeline/helpers"))
import portable_bundle as portable
import reported_science as science


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--positive-bundle", type=Path)
    parser.add_argument("--zero-bundle", type=Path)
    args = parser.parse_args()
    if bool(args.positive_bundle) != bool(args.zero_bundle):
        parser.error("Supply both real positive and zero bundles, or neither")
    args.output.mkdir(parents=True, exist_ok=False)
    results = []

    def check(name, call, expected=None):
        error = None
        try:
            call()
        except portable.BundleError as caught:
            error = str(caught)
        assert error is None if expected is None else error is not None and expected in error, (name, error)
        results.append(dict(name=name, passed=True, guard=error))

    def analyzer(trains):
        recording = si.NumpyRecording(np.zeros((200, 2), dtype="float32"), 1000)
        recording.set_channel_locations(np.asarray([[0, 0], [0, 20]]))
        if trains:
            sorting = si.NumpySorting.from_unit_dict(trains, 1000)
        else:
            empty = np.asarray([], dtype="int64")
            sorting = si.NumpySorting.from_samples_and_labels([empty], [empty], 1000, unit_ids=empty)
        return si.create_sorting_analyzer(sorting, recording, format="memory", sparse=False, return_in_uV=False)

    owner = analyzer({11: np.asarray([1, 4]), 22: np.asarray([3, 8])})
    times = {np.int64(11): np.asarray([0.001, 0.004]), np.int64(22): np.asarray([0.003, 0.008])}
    check("actual_integer_unit_trains", lambda: science.bind_trains(times, owner, "recording"))
    check("separate_report_selection", lambda: science.bind_trains({np.int64(22): times[22]}, owner, "recording"))
    check("empty_report_selection", lambda: science.bind_trains({}, owner, "recording"))
    zero = analyzer({})
    check("genuine_zero_analyzer", lambda: science.bind_trains({}, zero, "recording"))
    strings = analyzer({"alpha": np.asarray([1, 4]), "beta": np.asarray([3, 8])})
    check("actual_string_unit_trains", lambda: science.bind_trains({"alpha": times[11]}, strings, "recording"))
    for name, changed in [
        ("wrong_same_count_unit", {np.int64(12): times[11], np.int64(22): times[22]}),
        ("boolean_unit_alias", {True: times[11]}),
        ("floating_unit_alias", {11.0: times[11]}),
    ]:
        check(name, lambda changed=changed: science.bind_trains(changed, owner, "recording"), "unit")
    for name, values in [
        ("finite_in_bounds_time_change", np.asarray([0.002, 0.004])),
        ("out_of_bounds_time", np.asarray([0.001, 0.3])),
        ("nan_time", np.asarray([np.nan, 0.004])),
        ("infinite_time", np.asarray([np.inf, 0.004])),
        ("float32_times", times[11].astype("float32")),
        ("wrong_shape", times[11].reshape((1, 2))),
        ("truncated_train", times[11][:1]),
        ("non_array_times", times[11].tolist()),
    ]:
        check(name, lambda values=values: science.bind_trains({11: values}, owner, "recording"), "Report spike train differs")

    def burst(count=2):
        return dict(status="ok" if count else "no_spikes", n_units=count,
                    burstlets={"events": []}, network_bursts={"events": []}, superbursts={"events": []},
                    diagnostics={"threshold": 0.0}, plot_data={"values": [0.0, 1.0]})

    check("positive_burst", lambda: science.validate_bursts(burst(), times, "recording"))
    check("zero_burst", lambda: science.validate_bursts(burst(0), {}, "recording"))
    check("nonempty_train_unit_count", lambda: science.validate_bursts(burst(1), {11: times[11], 22: np.asarray([])}, "recording"))
    for family in ("burstlets", "network_bursts", "superbursts"):
        bad = burst(0); bad[family]["events"] = [{"start": 0.01, "end": 0.02}]
        check("false_empty_" + family, lambda bad=bad: science.validate_bursts(bad, {}, "recording"), "Nonempty burst events without spikes")
    for name, bad in [("boolean_burst_count", dict(burst(), n_units=True)),
                      ("wrong_burst_count", dict(burst(), n_units=1)),
                      ("wrong_burst_status", dict(burst(), status="no_spikes"))]:
        check(name, lambda bad=bad: science.validate_bursts(bad, times, "recording"), "Burst outcome differs")
    for name, left, right in [("integer_float_field", 1, 1.0), ("boolean_integer_field", True, 1),
                              ("signed_zero_field", 0.0, -0.0), ("ordered_events", [1, 2], [2, 1])]:
        assert science.field_identity(left) != science.field_identity(right), name
        results.append(dict(name=name, passed=True))
    check("nonfinite_json", lambda: science.field_identity(float("nan")), "nonfinite")

    data = args.output / "tiny-data"
    report = data / "reports/recording"; report.mkdir(parents=True)
    bursts = data / "bursts/recording"; bursts.mkdir(parents=True)
    summary = dict(n_units_total=2, n_units_curated=2, status="ok", thresholds={"snr": 0.0})

    def write_documents(current_times, current_summary, current_bursts):
        np.save(report / "spike_times.npy", current_times, allow_pickle=True)
        (report / "report_summary.json").write_text(json.dumps(current_summary))
        (bursts / "network_results.json").write_text(json.dumps(current_bursts))

    write_documents(times, summary, burst())
    record = dict(recording_ids=["recording"], metadata=dict(reported_science=science.capture(data, ["recording"])))
    check("complete_unchanged_receipt", lambda: science.verify(data, record))
    for name, change, expected in [
        ("changed_valid_selection", "selection", "Report unit/train scientific receipt differs"),
        ("changed_network_event", "event", "Burst scientific field differs"),
        ("changed_plot_value", "plot", "Burst scientific field differs"),
        ("changed_diagnostic_type", "diagnostic", "Burst scientific field differs"),
        ("changed_summary_type", "summary", "Report summary scientific field differs"),
    ]:
        t, s, b = deepcopy(times), deepcopy(summary), burst()
        if change == "selection": t.pop(11)
        elif change == "event": b["network_bursts"]["events"] = [{"start": 0.01, "end": 0.02}]
        elif change == "plot": b["plot_data"]["values"][0] = -0.0
        elif change == "diagnostic": b["diagnostics"]["threshold"] = 0
        else: s["thresholds"]["snr"] = 0
        write_documents(t, s, b)
        check(name, lambda: science.verify(data, record), expected)
    write_documents(times, summary, burst())
    for name, receipt in [("missing_receipt", None), ("boolean_schema", dict(record["metadata"]["reported_science"], schema_version=True)),
                          ("floating_schema", dict(record["metadata"]["reported_science"], schema_version=1.0))]:
        bad = deepcopy(record); bad["metadata"]["reported_science"] = receipt
        check(name, lambda bad=bad: science.verify(data, bad), "Missing or malformed reported scientific receipt")

    real = []
    for kind, bundle in (("positive", args.positive_bundle), ("zero", args.zero_bundle)):
        if bundle is None:
            continue
        before = hashlib.sha256((bundle / "bundle.json").read_bytes()).hexdigest()
        manifest = json.loads((bundle / "bundle.json").read_text())
        for recording in manifest["recording_ids"]:
            full = si.load_sorting_analyzer(bundle / "data" / ("postprocessed_" + recording + ".zarr"), load_extensions=False)
            original = np.load(bundle / "data/reports" / recording / "spike_times.npy", allow_pickle=True).item()
            science.bind_trains(original, full, recording)
            science.validate_bursts(json.loads((bundle / "data/bursts" / recording / "network_results.json").read_text()), original, recording)
        identity = science.capture(bundle / "data", manifest["recording_ids"])
        authored = deepcopy(manifest); authored["metadata"]["reported_science"] = identity
        science.verify(bundle / "data", authored)
        assert hashlib.sha256((bundle / "bundle.json").read_bytes()).hexdigest() == before
        real.append(dict(kind=kind, manifest_sha256=before, receipt=identity))
    output = dict(passed=True, policy_controls=results, real_input_policy_checks=real,
                  scientific_producers=0, full_bundle_API_CLI_NF_acceptance=False,
                  runtime=dict(uid=os.getuid(), gid=os.getgid(), numpy=np.__version__, threadpools=threadpool_info()))
    (args.output / "summary.json").write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps(dict(passed=True, policy_controls=len(results), real_inputs=len(real))))


if __name__ == "__main__":
    main()
