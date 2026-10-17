#!/usr/bin/env python3
"""Preserve a real direct-run artifact set, then verify it after isolated relocation.

build requires the original fixture/run mounts; verify requires only the moved
bundle and this repository's helper/test source. Run both in the pinned base image.
"""
import argparse
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import time


SOURCE = Path(__file__).resolve().parents[2]
module_spec = importlib.util.spec_from_file_location("portable_bundle", SOURCE / "pipeline/helpers/portable_bundle.py")
bundle = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(bundle)


def task_results(run, name):
    import csv

    with (run / "evidence/trace.tsv").open() as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    row = next(row for row in rows if row["name"].split(" (")[0] == name)
    prefix, suffix = row["hash"].split("/")
    candidates = list((run / "work" / prefix).glob(suffix + "*"))
    assert len(candidates) == 1
    return candidates[0] / "capsule/results"


def descriptors(source, destination, data_base):
    return bundle.capsule_serializations(source, destination, data_base)


def expect_error(label, function, contains, results):
    try:
        function()
    except bundle.BundleError as error:
        assert contains in str(error), (label, str(error))
        results[label] = str(error)
    else:
        raise AssertionError(f"{label}: invalid dependency was accepted")


def build(args):
    import numpy as np
    import spikeinterface as si
    import spikeinterface.extractors as se

    args.output.mkdir(parents=True, exist_ok=False)
    payloads = [{"source": str(args.source_session), "destination": "data/ecephys_session"}]
    serializations = []
    for name in ("job_dispatch", "preprocessing", "spikesort_kilosort4", "postprocessing", "curation", "visualization"):
        folder = task_results(args.reference_run, name)
        for path in sorted(folder.iterdir()):
            payloads.append({"source": str(path), "destination": "data/" + path.name})
        serializations.extend(descriptors(folder, Path("data"), folder.parent / "data"))
    jobs = task_results(args.reference_run, "job_dispatch")
    recording_ids = [json.loads(path.read_text())["recording_name"] for path in sorted(jobs.glob("job_*.json"))]
    prepared = task_results(args.reference_run, "preprocessing")
    lazy = si.load(prepared / ("preprocessed_" + recording_ids[0] + ".json"), base_folder=prepared)
    pickle_path = args.output / "preprocessed_pickle-regression.pkl"
    lazy.dump_to_pickle(pickle_path, relative_to=prepared)
    payloads.append({"source": str(pickle_path), "destination": "data/" + pickle_path.name})
    serializations.append({"format": "extractor_pickle", "source": str(pickle_path), "base": str(prepared),
                           "path": "data/" + pickle_path.name, "relative_to": "data"})
    spec = {"schema_version": 1, "unit_id": "reference-signed", "stage": "analyzed",
            "recording_ids": recording_ids, "payloads": payloads, "serializations": serializations,
            "metadata": {"test": "retained real phase 02 artifacts; no scientific stubs"}}
    (args.output / "assembly-spec.json").write_text(json.dumps(spec, indent=2))
    start = time.monotonic()
    record = bundle.pack(spec, args.output / "unit-a")
    original = bundle.verify(args.output / "unit-a")

    # A second independent unit has the identical raw basename and original
    # recording identity. Its outer unit identity, data and manifests stay separate.
    empty_folder = args.output / "empty-analyzer.zarr"
    recording = se.read_nwb_recording(args.source_session / "synthetic.nwb")
    empty = np.array([], dtype="int64")
    sorting = si.NumpySorting.from_samples_and_labels([empty], [empty], 30000, unit_ids=empty)
    si.create_sorting_analyzer(sorting=sorting, recording=recording, sparse=False, format="zarr", folder=empty_folder)
    empty_spec = {"schema_version": 1, "unit_id": "second-same-basename", "stage": "analyzed",
                  "recording_ids": recording_ids,
                  "payloads": [{"source": str(args.source_session), "destination": "data/ecephys_session"},
                               {"source": str(jobs / "job_0.json"), "destination": "data/job_0.json"},
                               {"source": str(empty_folder), "destination": "data/postprocessed_empty.zarr"}],
                  "serializations": [{"format": "analyzer_zarr", "source": str(empty_folder), "base": str(empty_folder),
                                      "path": "data/postprocessed_empty.zarr", "relative_to": "data/postprocessed_empty.zarr"},
                                     {"format": "extractor_json", "source": str(jobs / "job_0.json"), "base": str(jobs.parent / "data"),
                                      "path": "data/job_0.json", "pointer": ["recording_dict"], "relative_to": "data"}]}
    second = bundle.pack(empty_spec, args.output / "unit-b")
    assert (args.output / "unit-a/data/ecephys_session/synthetic.nwb").is_file()
    assert (args.output / "unit-b/data/ecephys_session/synthetic.nwb").is_file()
    assert record["unit_id"] != second["unit_id"]
    assert second["scientific_fingerprints"][0]["sorting"]["unit_ids"] == []
    assert second["scientific_fingerprints"][0]["sorting"]["spikes"]["shape"] == [0]

    failures = {}
    graph = {"class": "spikeinterface.extractors.nwbextractors.NwbRecordingExtractor", "kwargs": {"file_path": "absent.nwb"},
             "relative_paths": True}
    root = args.output / "unit-a"
    expect_error("missing_recording", lambda: bundle.rebase_extractor(graph, root, root, {}, root), "Missing recording dependency", failures)
    graph["kwargs"]["file_path"] = str(SOURCE / "pipeline/helpers/portable_bundle.py")
    expect_error("undeclared_recording", lambda: bundle.rebase_extractor(graph, root, root, {}, root), "Undeclared recording dependency", failures)
    expect_error("malformed_graph", lambda: bundle.rebase_extractor({}, root, root, {}, root), "Malformed extractor", failures)
    expect_error("escaping_destination", lambda: bundle.within(root, "../escape"), "Unsafe bundle path", failures)
    expect_error("missing_catalogue_entry", lambda: bundle.check_catalogue(record["serializations"][:-1], root / "data"), "missing from catalogue", failures)
    wrong = dict(record, recording_ids=["wrong-recording"])
    expect_error("mismatched_recording_identity", lambda: bundle.check_recording_ids(wrong, root / "data"), "do not match", failures)
    unsupported = deepcopy(record["serializations"])
    unsupported[0]["format"] = "unknown-codec"
    expect_error("unsupported_serialization", lambda: bundle.check_catalogue(unsupported, root / "data"), "Unknown executable serialization", failures)
    expect_error("output_inside_source", lambda: bundle.pack(spec, args.source_session / "invalid-nested-bundle"), "inside a declared source", failures)
    duplicate = args.output / "collision"
    duplicate.mkdir()
    source = args.output / "collision-source.txt"
    source.write_text("one recording")
    mappings, copied = {}, {}
    bundle.copy_payload(source, duplicate / "same.nwb", mappings, copied)
    expect_error("destination_collision", lambda: bundle.copy_payload(source, duplicate / "same.nwb", mappings, copied), "Destination collision", failures)
    expect_error("ambiguous_source_mapping", lambda: bundle.copy_payload(source, duplicate / "other.nwb", mappings, copied), "Ambiguous dependency destination", failures)
    result = {"passed": True, "real_artifacts": original, "second_same_basename_empty_unit": bundle.verify(args.output / "unit-b"),
              "negative_cases": failures, "duration_seconds": round(time.monotonic() - start, 2),
              "scientific_capsules_stubbed": False, "generated_pickle_fallback_tested": True,
              "isolated_relocation_pending": True}
    (args.output / "build-result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


def verify_moved(args):
    import hashlib

    root = args.bundle.resolve()
    original_spec = json.loads((root / "assembly-spec.json").read_text())
    assert all(not Path(item["source"]).exists() for item in original_spec["payloads"]), "Original producer payload is visible"
    results = {"real_unit": bundle.verify(root / "unit-a"), "empty_unit": bundle.verify(root / "unit-b")}
    record_path = root / "unit-a/bundle.json"
    record_bytes = record_path.read_bytes()
    record = json.loads(record_bytes)
    # A valid transport checksum is not enough: an executable graph must also
    # resolve entirely inside its declared bundle and preserve scientific identity.
    descriptor = next(item for item in record["serializations"] if item.get("pointer") == ["recording_dict"])
    path = root / "unit-a" / descriptor["path"]
    old = path.read_bytes()
    value = json.loads(old)
    graph = bundle.at_pointer(value, descriptor["pointer"])

    def replace_leaf(node):
        if isinstance(node, dict):
            if "file_path" in node:
                node["file_path"] = str(SOURCE / "pipeline/helpers/portable_bundle.py")
                return True
            return any(replace_leaf(child) for child in node.values())
        if isinstance(node, list):
            return any(replace_leaf(child) for child in node)
        return False

    assert replace_leaf(graph)
    path.write_bytes(bundle.encoded(value))
    failures = {}
    try:
        expect_error("changed_payload", lambda: bundle.verify(root / "unit-a"), "file inventory mismatch", failures)
        altered = deepcopy(record)
        key = path.relative_to(root / "unit-a/data").as_posix()
        altered["files"][key] = {"bytes": path.stat().st_size, "sha256": bundle.sha256(path)}
        altered.pop("content_sha256")
        altered["content_sha256"] = hashlib.sha256(bundle.encoded(altered)).hexdigest()
        record_path.write_bytes(bundle.encoded(altered))
        expect_error("escaped_dependency_even_with_updated_checksums", lambda: bundle.verify(root / "unit-a"), "Undeclared recording dependency", failures)
    finally:
        path.write_bytes(old)
        record_path.write_bytes(record_bytes)
    data_path = next((root / "unit-a/data").glob("preprocessed_*/traces_cached_seg0.raw"))
    missing = root / "temporarily-held-binary.raw"
    data_path.rename(missing)
    try:
        expect_error("missing_prepared_binary", lambda: bundle.verify(root / "unit-a"), "file inventory mismatch", failures)
    finally:
        missing.rename(data_path)
    results["negative_cases"] = failures
    results["passed"] = True
    results["original_producer_mount_available"] = False
    results["restored_bundle_valid"] = bundle.verify(root / "unit-a")["passed"]
    (root / "isolated-result.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("build")
    create.add_argument("--reference-run", type=Path, required=True)
    create.add_argument("--source-session", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    build(args) if args.command == "build" else verify_moved(args)


if __name__ == "__main__":
    main()
