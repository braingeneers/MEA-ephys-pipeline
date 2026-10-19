#!/usr/bin/env python3
"""Build and verify scientific stage boundaries over portable local bundles."""
import argparse
from copy import deepcopy
import importlib.util
import json
import math
from pathlib import Path

import portable_bundle as portable


PREVIOUS = {"prepared": "nwb", "sorted": "prepared", "analyzed": "sorted", "reported": "analyzed", "exported": "reported"}
ORDER = ["dispatch", "prepared", "sorted", "analyzed", "reported", "exported"]


def require(condition, message):
    if not condition:
        raise portable.BundleError(message)


def read_record(root):
    return json.loads((root / "bundle.json").read_text())


def classifier(path):
    spec = importlib.util.spec_from_file_location("mea_sorting_outcomes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.detected_no_spikes


def jobs(data):
    return [portable.read_serialization(path) for path in sorted(data.glob("job_*"))
            if path.suffix in portable.SERIALIZATION_SUFFIXES]


def checked_int(value, name):
    require(type(value) is int and value >= 0, f"{name} must be a nonnegative integer")
    return value


def scientific_policy(root, record, no_spikes):
    """Check expected recording sets and owned outcomes, independently of labels."""
    import numpy as np
    import spikeinterface as si

    data = root / "data"
    phase = record["metadata"].get("phase")
    require(phase in ORDER, "Missing or unknown scientific stage phase")
    ids = record["recording_ids"]
    dispatched = jobs(data)
    require(len(dispatched) == len(ids) and {job["recording_name"] for job in dispatched} == set(ids),
            "Stage recording set differs from dispatch")
    history = record["metadata"].get("completed_stages")
    require(history == ORDER[:ORDER.index(phase) + 1], "Incomplete or unordered stage history")
    provenance = record['metadata'].get('stage_provenance')
    require(isinstance(provenance,list) and [item.get('phase') for item in provenance] == history,
            'Missing or incomplete stage provenance')
    require(all(isinstance(item.get('processes'),list) and isinstance(item.get('context'),dict)
                and isinstance(item['context'].get('workflow_version'),str) and item['context']['workflow_version']
                for item in provenance), 'Malformed owning-stage execution provenance')
    require(record['metadata']['unit_meta'].get('unit_id') == record['unit_id'], 'Malformed unit metadata identity')
    duration = record["metadata"].get("max_duration_minutes")
    require(isinstance(duration, (int, float, str)) and not isinstance(duration, bool), "Missing declared recording duration")
    require(math.isfinite(float(duration)) and float(duration) > 0, "Invalid declared recording duration")
    outcomes = {}
    for recording in ids:
        job = next(job for job in dispatched if job["recording_name"] == recording)
        raw = si.load(job["recording_dict"], base_folder=data)
        raw.get_traces(start_frame=0, end_frame=min(8, raw.get_num_samples(0)))
        outcome = {"status": "dispatched", "frames": [raw.get_num_samples(i) for i in range(raw.get_num_segments())],
                   "sampling_frequency": raw.get_sampling_frequency()}
        if ORDER.index(phase) >= 1:
            prepared = data / ("preprocessed_" + recording)
            require(prepared.is_dir() and not (prepared / "error.txt").exists(), f"Preprocessing incomplete for {recording}")
            binary = next((data / ("binary_" + recording + suffix) for suffix in (".json", ".pkl", ".pickle")
                           if (data / ("binary_" + recording + suffix)).is_file()), None)
            require(binary is not None, f"Missing prepared recording descriptor for {recording}")
            rec = si.load(binary, base_folder=data)
            require([rec.get_num_samples(i) for i in range(rec.get_num_segments())] == outcome["frames"],
                    f"Prepared frame count changed for {recording}")
            require(rec.get_sampling_frequency() == outcome["sampling_frequency"], f"Prepared sampling frequency changed for {recording}")
            outcome["status"] = "prepared"
        if ORDER.index(phase) >= 2:
            folder = data / ("spikesorted_" + recording)
            require(folder.is_dir() and not (folder / "error.txt").exists(), f"Missing or failed sorting for {recording}")
            sorting = si.load(folder)
            spikes = sorting.to_spike_vector()
            log = json.loads((folder / 'spikeinterface_log.json').read_text())
            require(log.get('sorter_name') == 'kilosort4', f'Wrong sorting owner for {recording}')
            outcome.update(status="sorted", sorted_units=int(sorting.get_num_units()), sorted_spikes=len(spikes))
            if not len(spikes) and log.get('error') is True:
                status_path = folder / "sorting_status.json"
                require(status_path.is_file(), f"Empty sorting lacks owned terminal evidence for {recording}")
                status = json.loads(status_path.read_text())
                require(status.get("status") == "no_spikes_detected" and status.get("frames") == outcome["frames"]
                        and status.get("sample_rate") == outcome["sampling_frequency"], f"Malformed zero-detection outcome for {recording}")
                require(checked_int(status.get("units"), "sorting units") == 0 and checked_int(status.get("spikes"), "sorting spikes") == 0,
                        f"Nonempty zero-detection outcome for {recording}")
                require(sorting.get_num_units() == 0 and status.get("detector_log") == "spikeinterface_log.json",
                        f"Invalid empty sorting identity for {recording}")
                require(no_spikes(log),
                        f"Unrecognized sorter failure for {recording}")
                outcome["status"] = "no_spikes_detected"
            else:
                require(log.get('error') is False and isinstance(log.get('run_time'), (int, float))
                        and not isinstance(log['run_time'], bool) and math.isfinite(log['run_time']) and log['run_time'] >= 0,
                        f'Incomplete or failed sorting log for {recording}')
                if not len(spikes):
                    require(sorting.get_num_units() == 0, f'Empty sorting retains unexplained units for {recording}')
                    outcome['status'] = 'no_units_retained'
                else:
                    require(sorting.get_num_units() > 0, f"Spikes lack units for {recording}")
                for segment, frames in enumerate(outcome["frames"]):
                    samples = spikes["sample_index"][spikes["segment_index"] == segment]
                    require((samples >= 0).all() and (samples < frames).all(), f"Out-of-bounds spikes for {recording}")
        if ORDER.index(phase) >= 3:
            analyzer_path = data / ("postprocessed_" + recording + ".zarr")
            require((analyzer_path / ".zattrs").is_file(), f"Missing complete analyzer for {recording}")
            analyzer = si.load_sorting_analyzer(analyzer_path, load_extensions=False)
            require(analyzer.recording is not None, f"Analyzer lost recording for {recording}")
            require(all(analyzer.has_extension(name) for name in ('quality_metrics','template_metrics','templates','unit_locations')),
                    f'Analyzer lacks required reporting extensions for {recording}')
            qc_path = data / ("qc_" + recording + ".npy")
            require(qc_path.is_file(), f"Missing curation outcome for {recording}")
            qc = np.load(qc_path, allow_pickle=False)
            require(qc.dtype == np.dtype(bool) and qc.shape == (analyzer.get_num_units(),), f"Invalid curation mask for {recording}")
            visualization = data / ("visualization_" + recording)
            viz_metadata = data / ("data_process_visualization_" + recording + ".json")
            require(visualization.is_dir() and any(path.is_file() and path.stat().st_size for path in visualization.iterdir())
                    and viz_metadata.is_file(), f"Missing visualization for {recording}")
            require(isinstance(json.loads(viz_metadata.read_text()).get('output_parameters'), dict), f"Malformed visualization outcome for {recording}")
            curated_ids = analyzer.unit_ids[qc]
            outcome.update(analyzed_units=int(analyzer.get_num_units()), curated_units=int(qc.sum()),
                           curated_spikes=sum(len(analyzer.sorting.get_unit_spike_train(unit, segment_index=segment))
                                              for unit in curated_ids for segment in range(analyzer.sorting.get_num_segments())))
        if ORDER.index(phase) >= 4:
            report_path = data / "reports" / recording
            summary = json.loads((report_path / "report_summary.json").read_text())
            count = checked_int(summary.get("n_units_curated"), "report curated units")
            require(checked_int(summary.get("n_units_total"), "report total units") == outcome["analyzed_units"] and count <= outcome["analyzed_units"],
                    f"Report unit set differs from analyzer for {recording}")
            expected_status = "no_detected_units" if not outcome["analyzed_units"] else "ok" if count else "no_curated_units"
            require(summary.get("status") == expected_status, f"Invalid report terminal status for {recording}")
            times = np.load(report_path / "spike_times.npy", allow_pickle=True).item()
            require(isinstance(times, dict) and len(times) == count, f"Report spike dictionary differs from summary for {recording}")
            for values in times.values():
                values = np.asarray(values)
                require(values.ndim == 1 and np.isfinite(values).all() and (values >= 0).all(), f"Invalid reported spike times for {recording}")
            bursts = json.loads((data / "bursts" / recording / "network_results.json").read_text())
            has_spikes = any(len(values) for values in times.values())
            require(checked_int(bursts.get("n_units"), "burst units") == count and bursts.get("status") == ("ok" if has_spikes else "no_spikes"),
                    f"Burst outcome differs from report for {recording}")
            if not has_spikes:
                require(bursts.get("network_bursts", {}).get("events") == [], f"Nonempty burst events without spikes for {recording}")
            outcome.update(reported_units=count, report_status=summary["status"], burst_status=bursts["status"])
        outcomes[recording] = outcome
    if ORDER.index(phase) >= 3:
        collected = data / "collected"
        require(all((collected / name).is_dir() for name in ("preprocessed", "spikesorted", "postprocessed", "curated", "visualization")),
                "Incomplete collected artifact families")
        require((collected / "processing.json").is_file(), "Missing collected processing provenance")
    if ORDER.index(phase) >= 4:
        for family in ("reports", "bursts"):
            require({path.name for path in (data / family).iterdir() if path.is_dir()} == set(ids), f"Incomplete {family} recording set")
    if phase == "exported":
        exports = sorted((data / "derived-nwb").glob("*.nwb*"))
        require(exports, "Final export produced no derived NWB")
        units = spikes = 0
        for path in exports:
            if path.is_dir():
                import zarr
                group = zarr.open(str(path), mode="r")
                require("nwb_version" in group.attrs, f"Derived export is not NWB Zarr: {path.name}")
                if "units" in group:
                    units += len(group["units/id"])
                    spikes += len(group["units/spike_times"])
            else:
                import h5py
                with h5py.File(path, "r") as group:
                    require("nwb_version" in group.attrs, f"Derived export is not NWB HDF5: {path.name}")
                    if "units" in group:
                        units += len(group["units/id"])
                        spikes += len(group["units/spike_times"])
        require(units == sum(item["curated_units"] for item in outcomes.values())
                and spikes == sum(item["curated_spikes"] for item in outcomes.values()), "Derived NWB unit/spike inventory differs from curation")
    return outcomes


def verify_invocation_metadata(record, unit_meta):
    """Preserve predecessor claims while allowing new invocation metadata."""
    require(isinstance(unit_meta, dict), "Stage invocation unit metadata must be an object")
    previous = record.get("metadata", {}).get("unit_meta")
    require(isinstance(previous, dict), "Stage bundle lacks predecessor unit metadata")
    require(all(key in unit_meta and portable.encoded(unit_meta[key]) == portable.encoded(value)
                for key, value in previous.items()),
            "Stage invocation dropped or changed predecessor unit metadata")


def verify(root, expected, unit_id, no_spikes, unit_meta=None):
    portable.verify(root)
    record = read_record(root)
    require(record["stage"] == expected and record["unit_id"] == unit_id, "Bundle kind or unit identity differs from the requested input")
    if unit_meta is not None:
        verify_invocation_metadata(record, unit_meta)
    outcomes = scientific_policy(root, record, no_spikes)
    require(outcomes == record["metadata"].get("outcomes"), "Scientific outcome receipt differs from current payload")
    return record


def build(request, paths, output, no_spikes):
    require(request.get("schema_version") == 1 and request.get("phase") in ORDER, "Unsupported stage assembly request")
    require(len(paths) == request.get("input_count"), "Declared input count does not match staged paths")
    phase = request["phase"]
    previous = None
    payloads, descriptors, aliases = [], [], []
    metadata = {"phase": phase, "unit_meta": request["unit_meta"], "context": request["context"], "processes": request.get("processes", [])}
    require(metadata["unit_meta"].get("unit_id") == request["unit_id"], "Unit metadata differs from assembly identity")
    if phase != "dispatch":
        previous = paths[request["previous"]]
        prior = verify(previous, PREVIOUS[phase], request["unit_id"], no_spikes,
                       unit_meta=metadata["unit_meta"])
        require(prior["metadata"]["completed_stages"] == ORDER[:ORDER.index(phase)], "Predecessor history is incomplete")
        metadata['stage_provenance'] = deepcopy(prior['metadata']['stage_provenance'])
        payloads.append({"source": str(previous / "data"), "destination": "data"})
        for item in prior["serializations"]:
            descriptors.append(dict(item, source=str(previous / item["path"]), base=str(previous / item.get("relative_to", "data"))))
        ids = prior["recording_ids"]
        metadata.update(max_duration_minutes=prior["metadata"]["max_duration_minutes"],
                        predecessor={"kind": prior["stage"], "content_sha256": prior["content_sha256"]})
    else:
        source = paths[request["source"]]
        payloads.append({"source": str(source), "destination": "data/ecephys_session"})
        ids = [item["recording_name"] for item in jobs(paths[request["pieces"][0]["results"]])]
        metadata["max_duration_minutes"] = paths[request["duration"]].read_text().strip()
        metadata['stage_provenance'] = []
    for piece in request["pieces"]:
        source = paths[piece["results"]]
        destination = piece.get("destination", "data")
        payloads.append({"source": str(source), "destination": destination})
        require(source.is_dir(), "Declared scientific result is not a directory")
        layout = paths[piece["inputs"]]
        piece_descriptors = portable.capsule_serializations(source, destination, layout)
        if piece.get('role') == 'collector':
            require(piece.get('publication'), 'Collector needs its explicit publication serialization base')
            for descriptor in piece_descriptors:
                if descriptor['format'] == 'analyzer_zarr':
                    descriptor['base'] = str(Path(piece['publication']) / 'postprocessed' / Path(descriptor['source']).name)
        descriptors.extend(piece_descriptors)
        if piece.get("role") == "collector":
            for path in sorted((source / "preprocessed").glob("*.json")):
                descriptors.append({"format": "extractor_json", "source": str(path),
                                    "path": str(Path(destination) / path.relative_to(source)),
                                    "base": str(layout / "ecephys_session"), "relative_to": "data/ecephys_session"})
    # Declare equivalent producer-side copies explicitly. The portable layer
    # checks every byte (or unchanged JSON value) before adding these bindings.
    destinations = set()
    for payload in payloads:
        if payload['destination'] == 'data':
            destinations.update(path.name for path in Path(payload['source']).iterdir())
        elif Path(payload['destination']).parent == Path('data'):
            destinations.add(Path(payload['destination']).name)
    seen_aliases = set()
    for piece in request['pieces']:
        for path in paths[piece['inputs']].iterdir():
            if path.is_dir() and path.name in destinations:
                key = (str(path.resolve()), 'data/' + path.name)
                if key not in seen_aliases:
                    aliases.append({'source':key[0], 'destination':key[1]})
                    seen_aliases.add(key)
    metadata["completed_stages"] = ORDER[:ORDER.index(phase) + 1]
    metadata['stage_provenance'].append({'phase':phase,'context':deepcopy(metadata['context']),
                                         'processes':deepcopy(metadata['processes'])})
    spec = {"schema_version": 1, "unit_id": request["unit_id"], "stage": "nwb" if phase == "dispatch" else phase,
            "recording_ids": ids, "payloads": payloads, "serializations": descriptors, "metadata": metadata,
            "dependency_aliases":aliases}
    record = portable.pack(spec, output)
    outcomes = scientific_policy(output, record, no_spikes)
    record["metadata"]["outcomes"] = outcomes
    record.pop("content_sha256")
    import hashlib
    record["content_sha256"] = hashlib.sha256(portable.encoded(record)).hexdigest()
    (output / "bundle.json").write_bytes(portable.encoded(record) + b"\n")
    verify(output, record["stage"], record["unit_id"], no_spikes)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outcomes-code", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("build")
    create.add_argument("--request", type=Path, required=True)
    create.add_argument("--inputs", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--bundle", type=Path, required=True)
    check.add_argument("--kind", required=True)
    check.add_argument("--unit-id", required=True)
    check.add_argument("--unit-meta", type=Path,
                       help="Incoming unit metadata JSON; preserve predecessor claims before scientific work")
    args = parser.parse_args()
    no_spikes = classifier(args.outcomes_code)
    try:
        if args.command == "build":
            paths = [path.resolve() for path in sorted(args.inputs.glob("item*"))]
            record = build(json.loads(args.request.read_text()), paths, args.output, no_spikes)
        else:
            unit_meta = None
            if args.unit_meta is not None:
                unit_meta = json.loads(args.unit_meta.read_text())
                require(isinstance(unit_meta, dict), "Stage invocation unit metadata must be an object")
            record = verify(args.bundle.resolve(), args.kind, args.unit_id, no_spikes,
                            unit_meta=unit_meta)
    except (portable.BundleError, FileNotFoundError, json.JSONDecodeError) as error:
        parser.exit(1, f"Scientific stage boundary error: {error}\n")
    print(json.dumps({"unit_id": record["unit_id"], "kind": record["stage"], "bytes": record["bytes"], "verified": True}))


if __name__ == "__main__":
    main()
