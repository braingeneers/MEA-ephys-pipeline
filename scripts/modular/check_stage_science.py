#!/usr/bin/env python3
"""Compare complete reference science from fresh direct and named-stage runs.

Run --phase analyzers in the report image (the base scientific stack plus Excel
readers), then --phase nwb in the isolated NWB validator stack. Inputs are read-only.
This checks one recording per run; multi-unit isolation/relocation have separate
integration checks. Differences are recorded before the command exits nonzero.
"""
import argparse
from contextlib import ExitStack
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import uuid
import xml.etree.ElementTree as ET
import pickle
import traceback

import numpy as np
from science_provenance import canonical


def normalized(value):
    import pandas as pd

    if value is pd.NA:
        return {"pandas_na": True}
    if value is pd.NaT:
        return {"pandas_nat": True}
    if isinstance(value, np.generic):
        return normalized(value.item())
    if isinstance(value, np.ndarray):
        if value.dtype.hasobject or value.dtype.fields:
            content = normalized(value.tolist())
        else:
            array = value.copy() if value.dtype.kind in "fc" else value
            if value.dtype.kind in "fc":
                array[np.isnan(array)] = np.nan
            content = hashlib.sha256(array.tobytes(order="C")).hexdigest()
        return {"shape": list(value.shape), "dtype": value.dtype.descr if value.dtype.fields else value.dtype.str,
                "content": content}
    if isinstance(value, pd.DataFrame):
        return {"columns": normalized(value.columns.tolist()), "index": normalized(value.index.to_numpy()),
                "dtypes": [str(dtype) for dtype in value.dtypes],
                "columns_data": [normalized(value[key].to_numpy()) for key in value.columns]}
    if isinstance(value, dict):
        return {f"{type(key.item() if isinstance(key, np.generic) else key).__name__}:{key}": normalized(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalized(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite": str(value)}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if type(value).__module__.startswith("sklearn."):
        return {"model_class": type(value).__module__ + "." + type(value).__name__,
                "state": normalized(value.__getstate__())}
    raise TypeError(f"Unsupported scientific value: {type(value)}")


class Comparison:
    def __init__(self):
        self.checked = []
        self.differences = []
        self.execution_fields = []

    def same(self, name, left, right):
        a, b = normalized(left), normalized(right)
        self.checked.append(name)
        if a != b:
            self.differences.append({"field": name, "left": a, "right": b})

    def execution_time(self, name, value):
        datetime.fromisoformat(value)
        self.execution_fields.append(name)


def task(run, role):
    rows = list(csv.DictReader((run / "evidence/trace.tsv").open(), delimiter="\t"))
    rows = [row for row in rows if row["name"].split(" (")[0].split(":")[-1] == role]
    assert len(rows) == 1 and rows[0]["status"] == "COMPLETED" and rows[0]["exit"] == "0", (role, rows)
    prefix, suffix = rows[0]["hash"].split("/")
    paths = list((run / "work" / prefix).glob(suffix + "*"))
    assert len(paths) == 1
    return paths[0].resolve() / "capsule/results"


def only(pattern):
    paths = list(pattern)
    assert len(paths) == 1, paths
    return paths[0]


def compare_recordings(check, label, left, right):
    check.same(label + ".sampling_frequency", left.get_sampling_frequency(), right.get_sampling_frequency())
    check.same(label + ".channel_ids", left.channel_ids, right.channel_ids)
    check.same(label + ".properties", {key: left.get_property(key) for key in left.get_property_keys()},
               {key: right.get_property(key) for key in right.get_property_keys()})
    assert left.get_num_segments() == right.get_num_segments()
    for segment in range(left.get_num_segments()):
        frames = left.get_num_samples(segment)
        assert frames == right.get_num_samples(segment)
        check.same(f"{label}.times.{segment}", left.get_times(segment), right.get_times(segment))
        for start in range(0, frames, 60000):
            kwargs = dict(segment_index=segment, start_frame=start, end_frame=min(start + 60000, frames))
            for scaled in (False, True):
                check.same(f"{label}.samples.{segment}.{start}.scaled={scaled}",
                           left.get_traces(**kwargs, return_in_uV=scaled), right.get_traces(**kwargs, return_in_uV=scaled))


def compare_sortings(check, label, left, right):
    check.same(label + ".unit_ids", left.unit_ids, right.unit_ids)
    check.same(label + ".frequency", left.sampling_frequency, right.sampling_frequency)
    check.same(label + ".spikes", left.to_spike_vector(), right.to_spike_vector())
    check.same(label + ".properties", {key: left.get_property(key) for key in left.get_property_keys()},
               {key: right.get_property(key) for key in right.get_property_keys()})


def compare_zarr(check, label, left, right, nwb=False):
    import zarr

    def attributes(attrs, path):
        value = dict(attrs)
        if not nwb and "run_info" in value:
            value["run_info"] = dict(value["run_info"])
            elapsed = value["run_info"].pop("runtime_s", None)
            assert elapsed is None or elapsed >= 0
            check.execution_fields.append(label + "/" + path + ".run_info.runtime_s")
        if nwb:
            if path == "units" and "colnames" in value:
                assert len(value["colnames"]) == len(set(value["colnames"]))
                value["colnames"] = sorted(value["colnames"])
                check.execution_fields.append(label + "/units.colnames (named columns compared separately)")
            def references(item):
                if isinstance(item, dict):
                    result = {}
                    for key, val in item.items():
                        if key == "object_id" and val is not None:
                            uuid.UUID(val)
                            result[key] = "generated-container-id"
                        else:
                            result[key] = references(val)
                    return result
                if isinstance(item, list):
                    return [references(val) for val in item]
                return item
            value = references(value)
        return canonical(check, label + "/" + path, value) if not nwb else value

    def visit(a, b, path):
        check.same(label + "/" + path + ".attrs", attributes(a.attrs, path), attributes(b.attrs, path))
        assert isinstance(a, zarr.Array) == isinstance(b, zarr.Array), path
        if isinstance(a, zarr.Array):
            if nwb and path == "units/unit_name":
                for values in (a[:], b[:]):
                    assert len(set(values)) == len(values)
                    assert all(uuid.UUID(str(value)).version == 4 for value in values)
                check.execution_fields.append(label + "/units/unit_name (generated UUIDs)")
            else:
                first, second = a[:], b[:]
                if not nwb:
                    first = canonical(check, label + "/" + path, first)
                    second = canonical(check, label + "/" + path, second)
                check.same(label + "/" + path, first, second)
        else:
            check.same(label + "/" + path + ".keys", sorted(a.keys()), sorted(b.keys()))
            for key in sorted(set(a.keys()) & set(b.keys())):
                visit(a[key], b[key], path + "/" + key if path else key)
    visit(left, right, "")


def svg_content(path):
    root = ET.fromstring(path.read_bytes())
    identities = {node.attrib["id"]: f"id{index}" for index, node in enumerate(root.iter()) if "id" in node.attrib}
    for node in root.iter():
        if node.tag == "{http://purl.org/dc/elements/1.1/}date":
            datetime.fromisoformat(node.text)
            node.text = "execution-time"
        for key, value in list(node.attrib.items()):
            if key == "id":
                node.set(key, identities[value])
            else:
                node.set(key, re.sub(r"url\(#([^)]*)\)", lambda match: "url(#" + identities[match[1]] + ")", value))
                if value.startswith("#") and value[1:] in identities:
                    node.set(key, "#" + identities[value[1:]])
    return ET.tostring(root)


def compare_products(check, label, left, right, skip_zarr=False, source_directory=None):
    def files(root):
        return {str(path.relative_to(root)): path for path in root.rglob("*") if path.is_file()}
    a, b = files(left), files(right)
    check.same(label + ".files", sorted(a), sorted(b))
    for name in sorted(set(a) & set(b)):
        x, y = a[name], b[name]
        key = label + "/" + name
        if skip_zarr and any(part.endswith((".zarr", ".nwb")) for part in Path(name).parts):
            # Inventory still checks every format/chunk file. Logical datasets
            # and attributes are checked by analyzers/nwb in their pinned stacks.
            continue
        if x.suffix == ".log":
            assert x.stat().st_size and y.stat().st_size
            check.execution_fields.append(key + " (capsule log)")
        elif x.suffix == ".json":
            first, second = json.loads(x.read_text()), json.loads(y.read_text())
            if x.name in ("report_summary.json", "network_results.json"):
                for record in (first, second):
                    check.execution_time(key + ".timestamp", record.pop("timestamp"))
            if x.name == "report_summary.json":
                for record, path in ((first, x), (second, y)):
                    assert Path(record.pop("spike_times_saved")).name == "spike_times.npy"
                    assert (path.parent / "spike_times.npy").is_file()
            if x.name.startswith("data_process_"):
                for record in (first, second):
                    for field in ("start_date_time", "end_date_time"):
                        check.execution_time(key + "." + field, record.pop(field))
            if x.name == "data_description.json" and source_directory is not None:
                assert source_directory.is_dir()
                if not any((source_directory / filename).exists() for filename in ("data_description.json", "subject.json")):
                    for record in (first, second):
                        assert record["subject_id"] == "000000" and record["source_data"] is None
                        created = record.pop("creation_time")
                        check.execution_time(key + ".creation_time", created)
                        expected = "000000_" + datetime.fromisoformat(created).strftime("%Y-%m-%d_%H-%M-%S")
                        assert record.pop("name") == expected
                    check.execution_fields.append(key + ".name (proved generated fallback date; no authored description/subject)")
            if x.name == "run_times.json" and (x.parent.name.startswith("motion_") or "motion" in x.parts):
                assert first.keys() == second.keys() and all(value >= 0 for value in (*first.values(), *second.values()))
                check.execution_fields.append(key + " (motion step runtimes)")
            else:
                check.same(key, canonical(check, key, first), canonical(check, key, second))
        elif x.suffix == ".pkl":
            with x.open("rb") as first, y.open("rb") as second:
                check.same(key, canonical(check, key, pickle.load(first)), canonical(check, key, pickle.load(second)))
        elif x.suffix == ".npy":
            def read(path):
                value = np.load(path, allow_pickle=True)
                return value.item() if value.shape == () else value
            check.same(key, read(x), read(y))
        elif x.suffix == ".xlsx":
            import openpyxl

            def read(path):
                book = openpyxl.load_workbook(path, read_only=True, data_only=False)
                try:
                    return {sheet.title: list(sheet.values) for sheet in book}
                finally:
                    book.close()
            check.same(key, read(x), read(y))
            check.execution_fields.append(key + " (ZIP workbook metadata)")
        elif x.suffix == ".pdf":
            def read(path):
                data = path.read_bytes()
                pattern = rb"/CreationDate \(D:[^)]*\)"
                dates = re.findall(pattern, data)
                assert len(dates) == 1
                return re.sub(pattern, b"/CreationDate (D:20000101000000Z)", data)
            check.same(key, read(x), read(y))
            check.execution_fields.append(key + " (PDF creation date)")
        elif x.suffix == ".svg":
            check.same(key, svg_content(x), svg_content(y))
            check.execution_fields.append(key + " (SVG creation date/generated IDs)")
        else:
            check.same(key, x.read_bytes(), y.read_bytes())


def analyzers(args, check):
    import spikeinterface as si
    import zarr

    pairs = [args.direct, args.grouped]
    prepared = [si.load(only(task(run, "preprocessing").glob("binary_*.json")),
                        base_folder=task(run, "preprocessing")) for run in pairs]
    compare_recordings(check, "prepared", *prepared)
    sorting = [si.load(only(task(run, "spikesort_kilosort4").glob("spikesorted_*"))) for run in pairs]
    compare_sortings(check, "sorter", *sorting)
    paths = [only(task(run, "postprocessing").glob("postprocessed_*.zarr")) for run in pairs]
    values = [si.load_sorting_analyzer(path) for path in paths]
    compare_sortings(check, "analyzed.sorting", *[value.sorting for value in values])
    compare_recordings(check, "analyzed.recording", *[value.recording for value in values])
    compare_zarr(check, "analyzed", *[zarr.open(str(path), "r") for path in paths])
    collected = [only((task(run, "results_collector") / "postprocessed").glob("*.zarr")) for run in pairs]
    # API loading must read published recording dependencies, not just extension arrays.
    left_publication = ("results/postprocessed" if args.direct_layout == "direct" else
                        "results/export/bundles/reference.exported/data/collected/postprocessed")
    publication = [args.direct / left_publication / collected[0].name,
                   args.grouped / "results/export/bundles/reference.exported/data/collected/postprocessed" / collected[1].name]
    publication = [path.resolve() for path in publication]
    loaded = [si.load_sorting_analyzer(path) for path in publication]
    compare_recordings(check, "published.recording", *[value.recording for value in loaded])
    compare_sortings(check, "published.sorting", *[value.sorting for value in loaded])
    compare_zarr(check, "published", *[zarr.open(str(path), "r") for path in publication])
    for role in ("curation", "report_generation", "burst_detection", "visualization"):
        compare_products(check, role, *[task(run, role) for run in pairs])
    return {"frames": prepared[0].get_num_samples(), "channels": prepared[0].get_num_channels(),
            "units": sorting[0].get_num_units(), "spikes": len(sorting[0].to_spike_vector())}


def producers(args, check):
    roles = ("job_dispatch", "preprocessing", "spikesort_kilosort4", "postprocessing", "curation",
             "report_generation", "burst_detection", "visualization", "results_collector", "nwb_ecephys", "nwb_units")
    inventory = {}
    for role in roles:
        paths = [task(run, role) for run in (args.direct, args.grouped)]
        inventory[role] = [{str(path.relative_to(root)): {"bytes": path.stat().st_size,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                           for path in sorted(root.rglob("*")) if path.is_file()} for root in paths]
        if role in ("job_dispatch", "preprocessing", "spikesort_kilosort4", "postprocessing", "results_collector"):
            compare_products(check, "producer." + role, *paths, skip_zarr=True,
                             source_directory=args.source_nwb.parent if args.source_nwb else None)
        else:
            check.same("inventory." + role, sorted(inventory[role][0]), sorted(inventory[role][1]))
        scratch = [root.parent / "scratch" for root in paths]
        inventory[role + ".retained_scratch"] = [
            {str(path.relative_to(root)): {"bytes": path.stat().st_size,
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
             for path in sorted(root.rglob("*")) if path.is_file()} for root in scratch]
        compare_products(check, "scratch." + role, *scratch)
    return {"producer_inventory": inventory, "logical_zarr_checks_required": ["analyzers", "nwb"]}


def nwb(args, check):
    import zarr
    from pynwb import NWBHDF5IO
    from hdmf_zarr import NWBZarrIO
    from check_nwb_source_metadata import header

    with ExitStack() as stack:
        source = stack.enter_context(NWBHDF5IO(str(args.source_nwb), "r")).read()
        assert source.units is None, "This reference check excludes only newly generated unit UUIDs"
        for role in ("nwb_ecephys", "nwb_units"):
            paths = [only(task(run, role).glob("*.nwb")) for run in (args.direct, args.grouped)]
            values = [stack.enter_context(NWBZarrIO(str(path), "r")).read() for path in paths]
            for index, value in enumerate(values):
                check.same(f"{role}.authored_header.{index}", header(source), header(value))
                assert not value.acquisition and len(value.electrodes) == len(source.electrodes)
                for name in ("id", *source.electrodes.colnames):
                    a, b = source.electrodes[name][:], value.electrodes[name][:]
                    if name == "group":
                        a, b = [item.name for item in a], [item.name for item in b]
                    check.same(f"{role}.authored_electrodes.{index}.{name}", a, b)
            compare_zarr(check, role, *[zarr.open(str(path), "r") for path in paths], nwb=True)
    return {"authored_source_checked": True, "all_nwb_datasets_compared": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("analyzers", "producers", "nwb"), required=True)
    for name in ("direct", "grouped", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source-nwb", type=Path)
    parser.add_argument("--direct-layout", choices=("direct", "staged"), default="direct",
                        help="Published analyzer layout of the left run; staged also accepts independent-stage trace views")
    args = parser.parse_args()
    check = Comparison()
    detail, error, stack = {}, None, None
    try:
        detail = {"analyzers": analyzers, "producers": producers, "nwb": nwb}[args.phase](args, check)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        stack = traceback.format_exc()
    result = {"passed": not check.differences and error is None, "phase": args.phase, **detail,
              "checked": check.checked, "differences": check.differences,
              "incomplete_check_error": error,
              "incomplete_check_traceback": stack,
              "execution_or_representation_fields": sorted(set(check.execution_fields)),
              "numeric_tolerance": 0}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key not in
                     ("checked", "differences", "execution_or_representation_fields", "producer_inventory")}, indent=2))
    print("Differences:", [value["field"] for value in check.differences])
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
