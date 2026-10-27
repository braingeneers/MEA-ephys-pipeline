#!/usr/bin/env python3
"""Real NWB format controls for all-unit export with independent QC flags.

Uses tiny saved SI sortings and PyNWB writers, without running scientific capsules.
Every negative must remain readable and schema-valid before guard rejection.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import h5py
import numpy as np
import spikeinterface as si
import zarr
from pynwb import NWBFile, NWBHDF5IO, validate
from hdmf_zarr import NWBZarrIO

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / "pipeline/helpers"))
import exported_science as science
import portable_bundle as portable


def source(data, flags, offset=0., name="recording", dense=False):
    ids = np.asarray([10, 20, 30][:len(flags)], dtype="int64")
    trains = [np.asarray(x, dtype="int64") for x in ([1, 4, 600], [5, 9], [])][:len(flags)]
    if dense: trains[0] = np.arange(300, dtype="int64")
    sorting = si.NumpySorting.from_unit_dict(dict(zip(ids, trains)), 1000.)
    sorting.set_property("default_qc", np.asarray(flags, dtype=bool))
    sorting.save(folder=data / "collected/curated" / name)
    recording = si.NumpyRecording(np.zeros((1000, 2), dtype="int16"), 1000.)
    recording.set_times(np.arange(1000, dtype="float64") / 1000. + offset)
    return [(name, SimpleNamespace(unit_ids=sorting.unit_ids, sorting=sorting),
             recording, np.asarray(flags, dtype=bool))], trains


def write(path, backend, flags, trains, offset=0., operation=None):
    nwb = NWBFile("Unit inventory contract", "unit-inventory", datetime(2026, 1, 1, tzinfo=timezone.utc))
    ids = [10, 20, 30][:len(flags)]
    rows = [(u, flag, frames.astype("float64") / 1000. + offset) for u, flag, frames in zip(ids, flags, trains)]
    if operation == "missing_row": rows = rows[:-1]
    if operation == "extra_row": rows.append((99, False, np.asarray([0.1])))
    if operation == "swapped_unit_binding":
        rows[0] = (20, rows[0][1], rows[0][2])
        rows[1] = (10, rows[1][1], rows[1][2])
    if operation == "qc_flag": rows[1] = (rows[1][0], not rows[1][1], rows[1][2])
    if operation == "spike":
        rows[0][2][0] = 0.002 + offset
    if rows:
        nwb.add_unit_column("ks_unit_id", "Source sorting ID")
        nwb.add_unit_column("default_qc", "Source QC flag; rows are retained independently")
        for unit, flag, times in rows:
            nwb.add_unit(ks_unit_id=unit, default_qc=bool(flag), spike_times=times)
    cls = NWBHDF5IO if backend == "hdf5" else NWBZarrIO
    with cls(str(path), mode="w") as io:
        io.write(nwb, cache_spec=False)
    if backend == "zarr": zarr.consolidate_metadata(str(path))


def readable(path, backend):
    cls = NWBHDF5IO if backend == "hdf5" else NWBZarrIO
    with cls(str(path), mode="r") as io:
        io.read()
        errors = validate(io=io)
        assert not errors, errors


def mutate(path, backend, operation):
    store = h5py.File(path, "r+") if backend == "hdf5" else zarr.open_group(str(path), mode="r+")
    try:
        table = store["units"]
        if operation == "row_ids": table["id"][0] = 99
        elif operation == "index_dtype":
            values = table["spike_times_index"][:].astype("uint16")
            attrs = dict(table["spike_times_index"].attrs)
            del table["spike_times_index"]
            result = table.create_dataset("spike_times_index", data=values)
            result.attrs.update(attrs)
        elif operation == "tail":
            values = np.concatenate([table["spike_times"][:], [0.999]])
            if backend == "hdf5":
                attrs = dict(table["spike_times"].attrs)
                del table["spike_times"]
                target = table.create_dataset("spike_times", data=values)
                target.attrs.update(attrs)
                table["spike_times_index"].attrs["target"] = target.ref
            else:
                table["spike_times"].resize(len(values))
                table["spike_times"][:] = values
        else: raise AssertionError(operation)
    finally:
        if backend == "hdf5": store.close()
    if backend == "zarr": zarr.consolidate_metadata(str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    for backend in ("hdf5", "zarr"):
        for case, flags, offset in (("mixed", [True, False, False], 0.),
                ("all_pass", [True, True, True], 0.), ("all_fail", [False, False, False], 0.),
                ("empty", [], 0.), ("explicit_clock", [True, False, False], 0.25),
                ("uint16_index", [True, False, False], 0.)):
            data = args.output / backend / case / "data"
            (data / "derived-nwb").mkdir(parents=True)
            sources, trains = source(data, flags, offset, dense=case == "uint16_index")
            path = data / "derived-nwb/tiny.nwb"
            write(path, backend, flags, trains, offset)
            readable(path, backend)
            science.bind_units(data, sources)
            results.append(dict(backend=backend, case=case, passed=True, schema_valid=True))
        # Two stores/source sortings share IDs but retain distinct clocks/flags.
        data = args.output / backend / "multiple_sources/data"
        (data / "derived-nwb").mkdir(parents=True)
        sources = []
        for name, flags, offset in (("first", [True, False, False], 0.),
                                   ("second", [False, True, True], 0.25)):
            items, trains = source(data, flags, offset, name=name)
            sources.extend(items)
            path = data / "derived-nwb" / (name + ".nwb")
            write(path, backend, flags, trains, offset)
            readable(path, backend)
        science.bind_units(data, sources)
        results.append(dict(backend=backend, case="multiple_sources", passed=True, schema_valid=True))
        for operation, message in (("missing_row", "inventory differs"), ("extra_row", "differs from complete sorting"),
                ("swapped_unit_binding", "differs from complete sorting"), ("qc_flag", "differs from complete sorting"),
                ("spike", "differs from complete sorting"), ("row_ids", "dtype/shape/IDs"),
                ("index_dtype", "index dtype/values differ"), ("tail", "buffer tail")):
            data = args.output / backend / operation / "data"
            (data / "derived-nwb").mkdir(parents=True)
            flags = [True, False, False]
            sources, trains = source(data, flags)
            path = data / "derived-nwb/tiny.nwb"
            write(path, backend, flags, trains, operation=operation)
            if operation in ("row_ids", "index_dtype", "tail"): mutate(path, backend, operation)
            readable(path, backend)
            try: science.bind_units(data, sources)
            except portable.BundleError as error:
                assert message in str(error), (operation, str(error))
                results.append(dict(backend=backend, case=operation, passed=True, schema_valid=True, rejection=str(error)))
            else: raise AssertionError((backend, operation, "Invalid source inventory accepted"))
    result = dict(passed=True, controls=results, scientific_capsules_executed=0,
                  source_sortings_and_clocks_real=True, full_scientific_acceptance=False)
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(dict(passed=True, controls=len(results))))


if __name__ == "__main__":
    main()
