#!/usr/bin/env python3
"""Check real modeled NWB reads and schema rejection without writing the sources.

Run inside the candidate NWB image, with two tiny accepted converter outputs and
their complete authored projections. This checks reads only; real producer and
export/append validation are separate gates. Keep fixtures and results outside Git.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

import h5py
import numpy as np
from pynwb import NWBHDF5IO, validate
from spikeinterface.extractors import NwbRecordingExtractor

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "capsules/nwb_common/code"),
               str(ROOT / "pipeline/components/maxwell/scripts")]
from source_nwb import open_source_nwb
from cell_culture_projection import verify_cell_culture_projection


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_source(path, projection_path, work):
    before = sha(path)
    projection_before = sha(projection_path)
    projection = json.loads(projection_path.read_text())
    negative = []
    with open_source_nwb(path, "r", NWBHDF5IO) as io:
        nwb = io.read()
        differences = verify_cell_culture_projection(nwb, projection)
        assert differences == [], differences
        assert nwb.session_id == projection["context"]["session_id"]
        assert nwb.lab == projection["common"]["lab"]
        assert nwb.institution == projection["common"]["institution"]
        assert list(nwb.keywords) == projection["common"]["keywords"]
        assert list(nwb.experimenter) == [record["name"] for record in projection["people"].values()]
        es = nwb.acquisition["ElectricalSeries"]
        with h5py.File(path, "r") as source:
            raw = source["acquisition/ElectricalSeries/data"][:]
            assert np.array_equal(es.data[:], raw)
            assert es.conversion == source["acquisition/ElectricalSeries/data"].attrs["conversion"]
            for column, dataset in source["general/extracellular_ephys/electrodes"].items():
                if isinstance(dataset, h5py.Dataset) and dataset.dtype.kind in "fiub":
                    typed = nwb.electrodes.id[:] if column == "id" else nwb.electrodes[column][:]
                    assert np.array_equal(typed, dataset[:], equal_nan=True), column
        recording = NwbRecordingExtractor(file_path=str(path), load_time_vector=True)
        assert np.array_equal(recording.get_traces(), es.data[:])
        expected_times = es.timestamps[:] if es.timestamps is not None else (
            es.starting_time + np.arange(raw.shape[0]) / es.rate)
        assert np.array_equal(recording.get_times(), expected_times)
        del recording
        wrong = json.loads(json.dumps(projection))
        wrong["subject"]["subject_id"] = "incorrect-authored-subject"
        wrong_differences = verify_cell_culture_projection(nwb, wrong)
        assert "subject.subject_id" in wrong_differences, wrong_differences
        negative.append(dict(kind="wrong_authored_value", differences=wrong_differences))
        namespaces = {name: io.manager.type_map.namespace_catalog.get_namespace(name).version
                      for name in io.manager.type_map.namespace_catalog.namespaces}
    assert validate(path=path) == []
    for kind, namespace, version in (("future-core", "core", "999.0.0"),
                                     ("old-modeled-extension", "ndx-cell-culture", "0.9.0")):
        changed = work / (kind + ".nwb")
        shutil.copyfile(path, changed)
        with h5py.File(changed, "a") as source:
            root = source["specifications/" + namespace]
            old = list(root)[-1]
            dataset = root[old]["namespace"]
            stored = dataset[()]
            if hasattr(stored, "size") and stored.size == 1:
                stored = stored.item()
            content = json.loads(stored)
            for declaration in content["namespaces"]:
                if declaration["name"] == namespace:
                    declaration["version"] = version
            dataset[...] = json.dumps(content)
            root.move(old, version)
        try:
            with open_source_nwb(changed, "r", NWBHDF5IO) as io:
                io.read()
        except ValueError as error:
            assert "Unsupported cached NWB namespace" in str(error), (kind, str(error))
            negative.append(dict(kind=kind, error=str(error)))
        else:
            raise AssertionError("Incompatible cached namespace accepted")
    unknown = work / "unknown-namespace.nwb"
    shutil.copyfile(path, unknown)
    with h5py.File(unknown, "a") as source:
        source["general/subject"].attrs["namespace"] = "foreign-uncached-namespace"
    try:
        with open_source_nwb(unknown, "r", NWBHDF5IO) as io:
            io.read()
    except Exception as error:
        assert "foreign-uncached-namespace" in str(error), str(error)
        negative.append(dict(kind="unknown_namespace", error=str(error)))
    else:
        raise AssertionError("Unknown uncached namespace accepted")
    # A rejected file must not poison the next invocation's private map.
    with open_source_nwb(path, "r", NWBHDF5IO) as io:
        assert verify_cell_culture_projection(io.read(), projection) == []
    assert sha(path) == before and sha(projection_path) == projection_before
    return dict(input=str(path), input_sha256=before, projection_sha256=projection_before,
                model_differences=differences, neural_values=raw.size,
                frames=raw.shape[0], channels=raw.shape[1], namespaces=namespaces, negative=negative)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modeled", type=Path, nargs=2, action="append", required=True,
                        metavar=("NWB", "PROJECTION"))
    parser.add_argument("--work-dir", type=Path, required=True, help="Fresh negative-copy directory")
    parser.add_argument("--output", type=Path, required=True, help="New JSON evidence file")
    args = parser.parse_args()
    if args.work_dir.exists() or args.output.exists():
        parser.error("Choose fresh work and output paths.")
    args.work_dir.mkdir(parents=True)
    records = []
    for index, (source, projection) in enumerate(args.modeled):
        work = args.work_dir / str(index)
        work.mkdir()
        records.append(check_source(source, projection, work))
    report = dict(passed=True, modeled=records, export_append_or_producer_acceptance=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(passed=True, modeled_inputs=len(records),
                         neural_values=sum(record["neural_values"] for record in records),
                         negative_cases=sum(len(record["negative"]) for record in records)), indent=2))


if __name__ == "__main__":
    main()
