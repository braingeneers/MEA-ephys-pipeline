#!/usr/bin/env python3
"""Check source metadata and complete derived data in real NWB capsule outputs.

Run in the pinned NWB worker; use validate_nwb.py separately for schema validation.
All scientific inputs are read-only. Only --output and its temporary directory are
written. The baseline is the previous final NWB from the same analyzed recording.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile

import numpy as np
import spikeinterface as si
from pynwb import NWBFile, NWBHDF5IO
from pynwb.ecephys import LFP
from hdmf_zarr import NWBZarrIO

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capsules/nwb_common/code"))
from source_nwb import (source_nwb_from_jobs, electrode_groups_for_recording,
                        metadata_for_electrode_groups, apply_electrode_groups, devices_for_units)


def rejected(call, errors=(ValueError, FileNotFoundError)):
    try:
        call()
    except errors:
        return
    raise AssertionError("Malformed source identity was accepted")


def helper_checks(root):
    """Exercise nested bases, collisions, and real multi-device template mapping."""
    left, right = root / "a/source.nwb", root / "b/source.nwb"
    for path in (left, right):
        path.parent.mkdir()
        path.write_bytes(b"identity-only path fixture")

    def extractor(path):
        return {"class": "spikeinterface.extractors.NwbRecordingExtractor",
                "kwargs": {"file_path": str(path)}}

    graph = {"class": "spikeinterface.core.SelectSegmentRecording",
             "relative_paths": True, "kwargs": {"recording": extractor("a/source.nwb")},
             "annotations": {"historical_recording": extractor("b/source.nwb")}}
    jobs = [{"recording_dict": graph}]
    assert source_nwb_from_jobs(jobs, root) == left
    assert source_nwb_from_jobs(jobs * 2, root) == left
    absolute = {"recording_dict": extractor(left)}
    assert source_nwb_from_jobs([absolute], root / "b") == left
    rejected(lambda: source_nwb_from_jobs(jobs + [{"recording_dict": extractor(right)}], root))
    rejected(lambda: source_nwb_from_jobs([{"recording_dict": extractor("a/source.nwb")}], root))
    rejected(lambda: source_nwb_from_jobs([{"recording_dict": extractor(root / "missing.nwb")}], root))
    missing = {"recording_dict": {"class": "NwbRecordingExtractor", "kwargs": {}}}
    rejected(lambda: source_nwb_from_jobs([missing], root))
    non_nwb = {"recording_dict": {"class": "MaxwellRecordingExtractor", "kwargs": {"file_path": "x.h5"},
                                  "annotations": {"history": extractor(right)}}}
    assert source_nwb_from_jobs([non_nwb], root) is None
    store = root / "source.zarr"
    store.mkdir()
    rejected(lambda: source_nwb_from_jobs([{"recording_dict": extractor(store)}], root))
    (store / ".zattrs").write_text("{}")
    assert source_nwb_from_jobs([{"recording_dict": extractor(store)}], root) == store

    nwb = NWBFile("Hardware identity fixture", "hardware-check", datetime(2026, 1, 1, tzinfo=timezone.utc))
    nwb.add_electrode_column("channel_name", "Explicit source channel name")
    for index, group_name in enumerate(("left", "right")):
        device = nwb.create_device("hardware-" + group_name, description="Authored device")
        group = nwb.create_electrode_group(group_name, "Authored group", "Authored region", device)
        for offset, channel in enumerate(("A", "B")):
            nwb.add_electrode(id=2 * index + offset, group=group, channel_name=channel,
                              x=float(index), y=0.0, z=0.0, imp=np.nan, location="Authored region", filtering="none")
    traces = np.zeros((300, 4), dtype="int16")
    traces[[50, 100], 0] = -100
    traces[[80, 130], 3] = -200
    recording = si.NumpyRecording(traces, 1000)
    recording.set_dummy_probe_from_locations(np.c_[np.arange(4) * 20.0, np.zeros(4)])
    recording.set_property("channel_name", np.asarray(["A", "B", "A", "B"]))
    recording.set_property("group_name", np.asarray(["left", "left", "right", "right"]))
    groups = electrode_groups_for_recording(recording, nwb)
    assert groups == ["left", "left", "right", "right"]
    apply_electrode_groups(recording, groups)
    metadata = metadata_for_electrode_groups(nwb, groups)["Ecephys"]
    assert metadata["Device"] == [{"name": "hardware-left"}, {"name": "hardware-right"}]
    assert [item["location"] for item in metadata["ElectrodeGroup"]] == ["Authored region"] * 2
    sorting = si.NumpySorting.from_unit_dict({11: np.asarray([50, 100]), 22: np.asarray([80, 130])}, 1000)
    analyzer = si.create_sorting_analyzer(sorting, recording, format="memory", sparse=False, return_in_uV=False)
    analyzer.compute("random_spikes", seed=0)
    analyzer.compute("waveforms", ms_before=1.0, ms_after=2.0, n_jobs=1, progress_bar=False)
    analyzer.compute("templates")
    assert devices_for_units(analyzer, recording, nwb, groups).tolist() == ["hardware-left", "hardware-right"]
    recording.delete_property("group_name")
    recording.set_channel_groups([0] * 4)
    rejected(lambda: electrode_groups_for_recording(recording, nwb))
    return {"nested_relative_and_absolute_paths": True, "historical_annotations_ignored": True,
            "ambiguous_missing_and_undeclared_paths_rejected": True, "non_nwb_compatible": True,
            "duplicate_channel_names_require_groups": True, "multi_device_units": True}


def equal_values(left, right, label):
    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        assert len(left) == len(right), label
        for index, (a, b) in enumerate(zip(left, right)):
            equal_values(a, b, f"{label}[{index}]")
        return
    left, right = np.asarray(left), np.asarray(right)
    assert left.shape == right.shape, label
    if left.dtype.kind in "fc" and right.dtype.kind in "fc":
        assert np.array_equal(left, right, equal_nan=True), label
    elif left.dtype.kind == "O" and left.ndim:
        for index, (a, b) in enumerate(zip(left, right)):
            equal_values(a, b, f"{label}[{index}]")
    else:
        assert np.array_equal(left, right), label


def header(nwb):
    # Include every scalar/list field in the original NWBFile, plus Subject,
    # devices and groups. Container collections are checked as scientific data.
    omit = {"acquisition", "processing", "analysis", "electrodes", "units", "devices", "electrode_groups",
            "subject", "stimulus", "stimulus_template", "intervals", "scratch", "lab_meta_data"}
    def normalize(value):
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, np.ndarray)):
            return [normalize(item) for item in value]
        if isinstance(value, np.generic):
            return normalize(value.item())
        if hasattr(value, "name"):
            return value.name
        return value
    return normalize({"file": {key: value for key, value in nwb.fields.items() if key not in omit},
                      "subject": nwb.subject.fields if nwb.subject is not None else None,
                      "devices": {key: value.fields for key, value in nwb.devices.items()},
                      "electrode_groups": {key: value.fields for key, value in nwb.electrode_groups.items()}})


def lfp(nwb):
    series = [item for module in nwb.processing.values() for interface in module.data_interfaces.values()
              if isinstance(interface, LFP) for item in interface.electrical_series.values()]
    assert len(series) == 1
    return series[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "raw-output", "units-output", "baseline", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--flat-output", type=Path, help="Optional real export using the existing flat NWB input path")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    before = hashlib.sha256(args.source.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="nwb-metadata-check-", dir=args.output.parent) as temporary:
        helpers = helper_checks(Path(temporary))
    with ExitStack() as stack:
        def read(path):
            io = stack.enter_context((NWBZarrIO if path.is_dir() else NWBHDF5IO)(str(path), "r"))
            return io.read()
        source, raw, units, baseline = [read(path) for path in
                                       (args.source, args.raw_output, args.units_output, args.baseline)]
        authored = header(source)
        for target in (raw, units):
            assert header(target) == authored, "Authored source header changed"
            assert not target.acquisition, "Raw export is disabled in this fixture"
            assert len(target.electrodes) == len(source.electrodes)
            for name in ("id", *source.electrodes.colnames):
                if name == "group":
                    assert [item.name for item in target.electrodes[name][:]] == [item.name for item in source.electrodes[name][:]]
                else:
                    equal_values(source.electrodes[name][:], target.electrodes[name][:], "electrodes." + name)
            equal_values(lfp(target).data[:], lfp(baseline).data[:], "all LFP samples")
            equal_values(lfp(target).timestamps[:], lfp(baseline).timestamps[:], "all LFP timestamps")
        if args.flat_output:
            flat = read(args.flat_output)
            assert header(flat) == authored, "Flat input metadata changed"
            assert len(flat.electrodes) == len(source.electrodes)
            assert set(flat.acquisition) == set(source.acquisition), "Flat input lost existing acquisition"
            for name, series in source.acquisition.items():
                equal_values(series.data[:], flat.acquisition[name].data[:], "flat acquisition." + name)
            equal_values(lfp(flat).data[:], lfp(baseline).data[:], "flat LFP samples")
            equal_values(lfp(flat).timestamps[:], lfp(baseline).timestamps[:], "flat LFP timestamps")
        assert raw.units is None
        assert len(units.units) == len(baseline.units) > 0
        compared = []
        for name in ("id", *baseline.units.colnames):
            if name == "unit_name":  # The original exporter generates a UUID per row.
                continue
            if name == "electrodes":
                # Compare the complete ragged row selection. Dereferencing it
                # returns electrode DataFrames, whose authored group labels were
                # repaired and are checked against the source above.
                for column in ("data", "target"):
                    a, b = baseline.units[name], units.units[name]
                    a = a.data[:] if column == "data" else a.target.data[:]
                    b = b.data[:] if column == "data" else b.target.data[:]
                    equal_values(a, b, "units.electrodes." + column)
            else:
                equal_values(baseline.units[name][:], units.units[name][:], "units." + name)
            compared.append(name)
        spikes = sum(len(train) for train in units.units["spike_times"])
        result = {"passed": True, "helpers": helpers, "source_sha256": before, "authored_metadata": authored,
                  "electrodes": len(units.electrodes), "raw_export_disabled": True,
                  "lfp_shape": list(lfp(units).data.shape), "all_lfp_samples_and_times_equal": True,
                  "units": len(units.units), "spikes": spikes, "equal_unit_columns": compared,
                  "generated_unit_uuids_compared": False}
        result["flat_input_checked"] = args.flat_output is not None
    assert hashlib.sha256(args.source.read_bytes()).hexdigest() == before, "Source NWB was modified"
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
