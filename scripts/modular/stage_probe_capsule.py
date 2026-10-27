#!/usr/bin/env python3
"""Test-only scientific stubs; produce real tiny SI/NWB payloads for graph probes.

These are never installed in production capsules. Reporting and burst detection
remain the real capsules in the probe snapshot. This does not test KS4 science.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import time

import numpy as np
import spikeinterface as si


RID = "block0_acquisition-ElectricalSeriesRaw_recording1"


def write(path, value):
    path.write_text(json.dumps(value, indent=2, default=lambda item:item.tolist() if isinstance(item,np.ndarray) else item.item()))


def source_nwb(data):
    paths = sorted(path for path in (data / 'ecephys_session').glob('*.nwb') if path.is_file())
    if len(paths) != 1:
        raise ValueError('The graph probe requires one explicit source NWB.')
    return paths[0]


def completion(out, role):
    # Constructed test evidence, never a claim that a scientific capsule ran.
    name = 'preprocessing' if role == 'preprocessing' else 'spikesorting'
    parameters = {'recording_name': RID} if role == 'preprocessing' else {'sorter_name':'kilosort4'}
    write(out / f'data_process_{name}_{RID}.json', {
        'object_type':'Data process', 'process_type':'Ephys preprocessing' if role == 'preprocessing' else 'Spike sorting',
        'stage':'Processing', 'code':{'parameters':parameters}, 'output_parameters':{'graph_probe':True},
        'start_date_time':'2026-01-01T00:00:00+00:00', 'end_date_time':'2026-01-01T00:00:01+00:00'})


def fixture(root):
    from neuroconv.tools.spikeinterface import add_recording_to_nwbfile
    from probeinterface import generate_linear_probe
    from pynwb import NWBFile, NWBHDF5IO

    for unit, frames, value in (("slow", 60000, 1), ("fast", 75000, 2)):
        folder = root / unit
        folder.mkdir(parents=True)
        rec = si.NumpyRecording([np.full((frames, 8), value, dtype="int16")], 30000)
        rec.set_channel_gains(1.0)
        rec.set_channel_offsets(0.0)
        probe = generate_linear_probe(num_elec=8, ypitch=20)
        probe.set_device_channel_indices(np.arange(8))
        rec = rec.set_probe(probe)
        nwb = NWBFile(session_description="Stub-science stage graph probe",
                      identifier=unit, session_start_time=datetime(2026, 1, 1, tzinfo=timezone.utc))
        add_recording_to_nwbfile(rec, nwbfile=nwb)
        with NWBHDF5IO(folder / "recording.nwb", "w") as io:
            io.write(nwb)


def execute(role, argv):
    import spikeinterface.extractors as se

    data, out = Path("../data").resolve(), Path("../results").resolve()
    out.mkdir(exist_ok=True)
    jobs = sorted(data.glob("job_*.json"))
    raw = None
    if role == "job_dispatch":
        import h5py
        path = source_nwb(data)
        raw = se.read_nwb_recording(path)
        with h5py.File(path) as nwb:
            unit = nwb["identifier"][()].decode()
        if unit == "slow":
            time.sleep(4)
        write(out / "job_0.json", {"session_name":unit, "recording_name":RID,
              "recording_dict":raw.to_dict(recursive=True, relative_to=data), "skip_times":False})
    elif jobs:
        job = json.loads(jobs[0].read_text())
        unit = job["session_name"]
        if role != 'kilosort4':
            raw = si.load(job["recording_dict"], base_folder=data)
    else:
        unit = "analyzer-only"
    if role == "preprocessing":
        saved = raw.save(folder=out / ("preprocessed_" + RID), n_jobs=1)
        saved.dump_to_json(out / ("binary_" + RID + ".json"), relative_to=out)
        raw.dump_to_json(out / ("preprocessed_" + RID + ".json"), relative_to=out)
    elif role == "kilosort4":
        binary = si.load(data / ("binary_" + RID + ".json"), base_folder=data)
        empty = np.array([], dtype="int64")
        sorting = si.NumpySorting.from_samples_and_labels([empty], [empty], binary.get_sampling_frequency(), unit_ids=empty)
        folder = out / ("spikesorted_" + RID)
        sorting.save(folder=folder)
        # Deliberately exercise the precise owned zero-detection outcome. The
        # probe does not execute Kilosort, so this is constructed test evidence.
        write(folder / "spikeinterface_log.json", {"sorter_name":"kilosort4", "error":True,
              "error_trace":["ValueError: No spikes detected, cannot continue sorting."],
              "runtime_trace":["0 spikes extracted in 0.01s", "st0 shape: (0, 6)"]})
        write(folder / "sorting_status.json", {"status":"no_spikes_detected", "units":0, "spikes":0,
              "empty_units":0, "frames":[binary.get_num_samples()], "sample_rate":float(binary.get_sampling_frequency()),
              "detector_log":"spikeinterface_log.json"})
    elif role == "postprocessing":
        sorting = si.load(data / ("spikesorted_" + RID))
        analyzer = si.create_sorting_analyzer(sorting=sorting, recording=raw, sparse=False,
                                             format="zarr", folder=out / ("postprocessed_" + RID + ".zarr"))
        analyzer.compute("random_spikes", method="all")
        analyzer.compute("waveforms", n_jobs=1)
        analyzer.compute("templates")
        analyzer.compute("noise_levels", chunk_size=1000)
        analyzer.compute("unit_locations")
        analyzer.compute("template_metrics")
        analyzer.compute("quality_metrics", metric_names=["num_spikes","firing_rate","presence_ratio"])
    elif role == "curation":
        analyzer = si.load_sorting_analyzer(data / ("postprocessed_" + RID + ".zarr"))
        np.save(out / ("qc_" + RID + ".npy"), np.zeros(analyzer.get_num_units(), dtype=bool))
        # Explicit graph-probe data only; this does not execute a classifier.
        rows = ["decoder_label,decoder_probability"] + ["stub,1.0"] * analyzer.get_num_units()
        (out / ("unit_classifier_" + RID + ".csv")).write_text("\n".join(rows) + "\n")
    elif role == "visualization":
        folder = out / ("visualization_" + RID)
        folder.mkdir()
        (folder / "stub.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>stub</text></svg>')
        write(out / ("data_process_visualization_" + RID + ".json"), {"output_parameters":{"stub":True}})
    elif role == "results_collector":
        import zarr
        p = argparse.ArgumentParser()
        p.add_argument("--pipeline-data-path", type=Path)
        p.add_argument("--pipeline-results-path", type=Path)
        args = p.parse_args(argv)
        for name in ("preprocessed","spikesorted","postprocessed","curated","visualization"):
            (out / name).mkdir()
        for path in data.glob("preprocessed_*.json"):
            rec = si.load(path, base_folder=data)
            rec.dump_to_json(out / "preprocessed" / path.name, relative_to=args.pipeline_data_path)
        for pattern, family in (("spikesorted_*","spikesorted"), ("postprocessed_*.zarr","postprocessed"),
                                ("qc_*.npy","curated"), ("visualization_*","visualization")):
            for path in data.glob(pattern):
                target = out / family / path.name
                if path.is_dir():
                    shutil.copytree(path, target)
                else:
                    shutil.copy2(path, target)
                if family == "postprocessed":
                    from spikeinterface.core.core_tools import extractor_dict_iterator, set_value_in_extractor_dict
                    analyzer = si.load_sorting_analyzer(path)
                    assert analyzer.recording is not None
                    # Match the collector's all-unit sorting/QC output contract.
                    rid = path.stem[len("postprocessed_"):]
                    analyzer.sorting.set_property("default_qc", np.load(data / ("qc_" + rid + ".npy")))
                    analyzer.sorting.save(folder=out / "curated" / rid)
                    group = zarr.open(str(target), mode="r+")
                    value = zarr.open(str(path),mode='r')['recording'][0]
                    base = args.pipeline_results_path / family / path.name
                    for item in list(extractor_dict_iterator(value)):
                        if item.name in ('file_path','folder_path'):
                            original = path.resolve()/item.value if value.get('relative_paths') else Path(item.value)
                            set_value_in_extractor_dict(value,item.access_path,os.path.relpath(original.resolve(),base))
                    value['relative_paths'] = True
                    group["recording"][0] = value
                    zarr.consolidate_metadata(group.store)
        write(out / "processing.json", {"stub":True,"unit":unit})
    elif role in ("nwb_ecephys","nwb_units"):
        shutil.copy2(source_nwb(data), out / (role + ".nwb"))
    if role in ('preprocessing','kilosort4'):
        completion(out, role)
    write(out / ("probe_" + role + ".json"), {"role":role,"unit":unit,"argv":argv,
          "threads":{key:os.environ.get(key) for key in ("CO_CPUS","N_JOBS_EXT","OMP_NUM_THREADS",
                                                       "OPENBLAS_NUM_THREADS","MKL_NUM_THREADS","NUMBA_NUM_THREADS")}})


if __name__ == "__main__":
    if sys.argv[1] == "--fixture":
        fixture(Path(sys.argv[2]))
    else:
        execute(sys.argv[1], sys.argv[2:])
