#!/usr/bin/env python3
"""Schema-valid tiny format controls for complete derived-science receipts.

These contract fixtures are separate from full-duration scientific acceptance.
No scientific capsule, sorter, postprocessor or report renderer is executed.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys

import h5py
import numpy as np
import zarr
from pynwb import NWBFile, NWBHDF5IO, validate
from pynwb.file import Subject
from pynwb.ecephys import ElectricalSeries, LFP
from hdmf_zarr import NWBZarrIO

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / "pipeline/helpers"))
import exported_science as science
import portable_bundle as portable


def fixture():
    nwb = NWBFile("Tiny export contract fixture", "tiny-contract", datetime(2026, 1, 1, tzinfo=timezone.utc))
    nwb.subject = Subject(subject_id="subjectA", description="Contract fixture")
    for index in range(2):
        device = nwb.create_device("device" if index == 0 else "device1", description="Fixture device")
        group = nwb.create_electrode_group("group" + str(index), "Fixture group", "Fixture region", device)
        nwb.add_electrode(id=index, x=float(index), y=0., z=0., imp=np.nan,
                          location="Fixture region", filtering="none", group=group)
    nwb.add_acquisition(ElectricalSeries(name="Raw", data=np.arange(200, dtype="int16").reshape(100, 2),
        electrodes=nwb.create_electrode_table_region([0, 1], "Both electrodes"), starting_time=0., rate=1000.))
    module = nwb.create_processing_module("ecephys", "Fixture ephys")
    lfp = LFP(); module.add(lfp)
    lfp.add_electrical_series(ElectricalSeries(name="LFP", data=np.arange(40, dtype="int16").reshape(20, 2),
        electrodes=nwb.create_electrode_table_region([0, 1], "LFP electrodes"), starting_time=0., rate=200.))
    nwb.add_unit_column("snr", "Fixture quality metric")
    nwb.add_unit(id=11, spike_times=np.asarray([0.001, 0.004]), electrodes=[0], snr=3.25)
    nwb.add_unit(id=22, spike_times=np.asarray([0.003, 0.008]), electrodes=[1], snr=4.5)
    waves = np.arange(16, dtype="float64").reshape(2, 4, 2) / 10
    nwb.add_unit_column("waveform_mean", "Fixture waveforms", data=waves)
    nwb.add_unit_column("waveform_sd", "Fixture waveform SD", data=waves + 1)
    return nwb


def inventory(root):
    assert not any(p.is_symlink() for p in root.rglob("*"))
    return {p.relative_to(root).as_posix(): {"bytes": p.stat().st_size, "sha256": portable.sha256(p)}
            for p in sorted(root.rglob("*")) if p.is_file()}


def readable(path, backend):
    cls = NWBHDF5IO if backend == "hdf5" else NWBZarrIO
    with cls(str(path), mode="r") as io:
        nwb = io.read()
        errors = validate(io=io)
        assert not errors, errors
        assert len(nwb.units) == 2


def mutation(path, backend, operation):
    raw = "acquisition/Raw/data"
    lfp = "processing/ecephys/LFP/LFP/data"
    targets = {"unit": ("units/id", (0,), 12), "spike": ("units/spike_times", (0,), 0.002),
               "metric": ("units/snr", (0,), 3.5), "waveform": ("units/waveform_mean", (0, 0, 0), 0.125),
               "raw": (raw, (0, 0), 1), "lfp": (lfp, (0, 0), 1)}
    cls = h5py.File if backend == "hdf5" else zarr.open_group
    with_store = cls(str(path), mode="r+")
    try:
        store = with_store
        if operation in targets:
            target, index, value = targets[operation]
            assert store[target][index] != value
            store[target][index] = value
        elif operation == "subject":
            target = store["general/subject/subject_id"]
            if backend == "hdf5": target[()] = "SubjectA"
            else: target[0] = "SubjectA"
        elif operation == "reference":
            target = store["general/extracellular_ephys/electrodes/group"]
            values = target[:]
            target[:] = values[::-1]
        elif operation in ("attribute", "stale_metadata"):
            store[lfp].attrs["conversion"] = 2.0
        elif operation == "soft_link":
            name = "general/extracellular_ephys/group0"
            if backend == "hdf5":
                del store[name + "/device"]
                store[name]["device"] = h5py.SoftLink("/general/devices/device1")
            else:
                links = deepcopy(store[name].attrs["zarr_link"])
                link, = [link for link in links if link["name"] == "device"]
                link["path"] = "/general/devices/device1"
                store[name].attrs["zarr_link"] = links
        elif operation == "hard_link":
            assert backend == "hdf5"
            # DataSpec accepts a shared array. A hard-linked owned Device group
            # cannot replace the required LinkSpec named device in HDMF.
            name = lfp
            del store[name]
            store[name] = store[raw]
        else:
            raise AssertionError(operation)
    finally:
        if backend == "hdf5": with_store.close()
    if backend == "zarr" and operation != "stale_metadata":
        zarr.consolidate_metadata(str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--unchanged-bundle", type=Path)
    parser.add_argument("--subject-control-bundle", type=Path)
    args = parser.parse_args()
    if bool(args.unchanged_bundle) != bool(args.subject_control_bundle):
        parser.error("Supply both existing real bundles, or neither")
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    for backend, cls in (("hdf5", NWBHDF5IO), ("zarr", NWBZarrIO)):
        original = args.output / backend / "original/data"
        (original / "derived-nwb").mkdir(parents=True)
        path = original / "derived-nwb/tiny.nwb"
        with cls(str(path), mode="w") as io:
            # Installed core schemas suffice for tiny core-only contract fixtures.
            # Actual full cached-schema scientific artifacts retain their own proof.
            io.write(fixture(), cache_spec=False)
        if backend == "zarr": zarr.consolidate_metadata(str(path))
        readable(path, backend)
        expected = science.capture(original)
        record = dict(metadata=dict(derived_science=expected))
        science.verify(original, record)
        before = inventory(original)
        operations = ["unit", "spike", "metric", "waveform", "raw", "lfp", "subject", "reference", "attribute", "soft_link"]
        operations += ["hard_link"] if backend == "hdf5" else ["stale_metadata"]
        for operation in operations:
            data = args.output / backend / operation / "data"
            shutil.copytree(original, data, copy_function=shutil.copyfile)
            assert inventory(data) == before
            candidate = data / "derived-nwb/tiny.nwb"
            mutation(candidate, backend, operation)
            assert inventory(data) != before
            try:
                readable(candidate, backend)
            except Exception as error:
                (data.parent / "reader-failure.json").write_text(json.dumps(dict(
                    backend=backend, operation=operation, type=type(error).__name__, error=str(error),
                    scientific_rejection_passed=False), indent=2) + "\n")
                raise
            try: science.verify(data, record)
            except portable.BundleError as error: message = str(error)
            else: raise AssertionError((backend, operation, "Changed data accepted"))
            if operation == "stale_metadata":
                assert message.startswith("Consolidated NWB metadata differs from physical metadata"), message
            else:
                assert message.startswith("Derived NWB scientific ") and "differs" in message, message
                target = {"unit": "/units/id", "spike": "/units/spike_times", "metric": "/units/snr",
                          "waveform": "/units/waveform_mean", "raw": "/acquisition/Raw/data",
                          "lfp": "/processing/ecephys/LFP/LFP/data", "subject": "/general/subject/subject_id",
                          "reference": "/general/extracellular_ephys/electrodes/group",
                          "attribute": "/processing/ecephys/LFP/LFP/data",
                          "soft_link": "/general/extracellular_ephys/group0" + ("/device" if backend == "hdf5" else ""),
                          "hard_link": "/processing/ecephys/LFP/LFP/data"}[operation]
                assert ":" + target in message, (operation, message)
            assert inventory(original) == before
            result = dict(backend=backend,operation=operation,passed=True,schema_valid=True,guard=message)
            (data.parent / "control-result.json").write_text(json.dumps(result, indent=2) + "\n")
            results.append(result)
        for name, schema in (("boolean_schema", True), ("floating_schema", 1.0)):
            bad = deepcopy(record); bad["metadata"]["derived_science"]["schema_version"] = schema
            try: science.verify(original, bad)
            except portable.BundleError as error: assert str(error) == "Missing or malformed derived scientific receipt"
            else: raise AssertionError((backend, name))
            results.append(dict(backend=backend, operation=name, passed=True))
        for name, metadata in (("missing_receipt", {}), ("null_receipt", dict(derived_science=None))):
            try: science.verify(original, dict(metadata=metadata))
            except portable.BundleError as error: assert str(error) == "Missing or malformed derived scientific receipt"
            else: raise AssertionError((backend, name))
            results.append(dict(backend=backend, operation=name, passed=True))
        if backend == "zarr":
            for presence in (1, 1.0):
                bad = deepcopy(record); bad["metadata"]["derived_science"]["outputs"][0]["consolidated_metadata"] = presence
                try: science.verify(original, bad)
                except portable.BundleError as error: assert "consolidated metadata presence differs" in str(error)
                else: raise AssertionError("Untyped consolidated-presence alias accepted")
                results.append(dict(backend=backend, operation="typed_presence_"+type(presence).__name__, passed=True))
    for dtype in ("float32", "float64", "complex64", "complex128"):
        left = np.asarray(-0., dtype=dtype)[()]; right = np.asarray(0., dtype=dtype)[()]
        assert science.encoded(science.typed(left)) != science.encoded(science.typed(right)), dtype
    stream_cases = [np.arange(65537 * 2, dtype="int16").reshape(65537, 2),
                    np.arange(science.BLOCK_BYTES // 2 + 3, dtype="int16").reshape(1, -1),
                    np.empty((0, 2), dtype="float64"), np.asarray(-0., dtype="float64")]
    for values in stream_cases:
        observed = science.array_identity(values)
        assert observed["shape"] == list(values.shape) and observed["dtype"] == values.dtype.str
        assert observed["sha256"] == hashlib.sha256(values.tobytes(order="C")).hexdigest()
    real = None
    if args.unchanged_bundle:
        before = inventory(args.unchanged_bundle)
        bad_before = inventory(args.subject_control_bundle)
        expected = science.capture(args.unchanged_bundle / "data")
        record = dict(metadata=dict(derived_science=expected))
        science.verify(args.unchanged_bundle / "data", record)
        try: science.verify(args.subject_control_bundle / "data", record)
        except portable.BundleError as error:
            message = str(error)
            assert "scientific data differs" in message and "/general/subject/subject_id" in message, message
        else: raise AssertionError("Real subject control accepted")
        assert inventory(args.unchanged_bundle) == before and inventory(args.subject_control_bundle) == bad_before
        real = dict(unchanged_passed=True, same_shape_subject_rejected=True, guard=message,
                    full_original_and_candidate_posthashes_exact=True)
    result = dict(passed=True, controls=results, numeric_scalar_signed_zero_controls=4, full_stream_controls=4, real_input=real,
                  scientific_capsules_executed=0, full_stage_API_CLI_NF_acceptance=False)
    (args.output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(dict(passed=True, controls=len(results), real_input=real)))


if __name__ == "__main__":
    main()
