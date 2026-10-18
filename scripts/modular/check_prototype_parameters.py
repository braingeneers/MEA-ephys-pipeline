#!/usr/bin/env python3
"""Exercise JSON prototype validation and the real pinned SI localization API."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

import numpy as np
import spikeinterface as si

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capsules/postprocessing/code"))
from parameters import analyzer_compute_parameters


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    vector = (-np.exp(-(np.arange(-15, 15) / 6) ** 2)).astype("float32")
    parameters = {"spike_locations": {"method": "grid_convolution",
                                      "method_kwargs": {"prototype": vector.tolist()}}}
    before = json.dumps(parameters, sort_keys=True)
    converted = analyzer_compute_parameters(parameters, 30000)
    assert json.dumps(parameters, sort_keys=True) == before, "Submitted JSON provenance changed"
    actual = converted["spike_locations"]["method_kwargs"]["prototype"]
    assert np.array_equal(actual, vector) and actual.dtype == np.dtype("float32")

    unchanged = [{}, {"spike_locations": {"method": "grid_convolution"}},
                 {"spike_locations": {"method": "grid_convolution", "method_kwargs": {}}},
                 {"spike_locations": {"method": "grid_convolution", "method_kwargs": {"prototype": None}}}]
    for value in unchanged:
        original = deepcopy(value)
        assert analyzer_compute_parameters(value, 30000) == original and value == original

    invalid = {"empty": [], "nested": [vector.tolist()], "short": [1.0] * 29,
               "long": [1.0] * 31, "boolean": [True] * 30, "mixed_boolean": [False] + [1.0] * 29,
               "string": ["1"] * 30, "nan": [np.nan] * 30, "infinity": [np.inf] * 30,
               "float32_overflow": [1e50] * 30}
    for name, value in invalid.items():
        malformed = deepcopy(parameters)
        malformed["spike_locations"]["method_kwargs"]["prototype"] = value
        try:
            analyzer_compute_parameters(malformed, 30000)
        except ValueError as error:
            assert "postprocessing.spike_locations.method_kwargs.prototype" in str(error), (name, str(error))
        else:
            raise AssertionError("Accepted malformed prototype: " + name)
    integer = deepcopy(parameters)
    integer["spike_locations"]["method_kwargs"]["prototype"] = [-1] * 30
    assert analyzer_compute_parameters(integer, 30000)["spike_locations"]["method_kwargs"]["prototype"].dtype == np.dtype("float32")

    traces = np.zeros((3000, 2), dtype="int16")
    trains = {0: np.asarray([100, 1000, 2000]), 1: np.asarray([400, 1400, 2400])}
    for unit, times in trains.items():
        for peak in times:
            traces[peak - 15:peak + 15, unit] = (vector * 100).astype("int16")
    recording = si.NumpyRecording(traces, 30000)
    recording.set_dummy_probe_from_locations(np.c_[np.arange(2) * 20.0, np.zeros(2)])
    sorting = si.NumpySorting.from_unit_dict(trains, 30000)
    analyzer = si.create_sorting_analyzer(sorting, recording, format="memory", sparse=False, return_in_uV=False)
    analyzer.compute("random_spikes", method="all")
    analyzer.compute("templates", ms_before=0.5, ms_after=0.5, n_jobs=1, progress_bar=False)
    try:
        analyzer.compute("spike_locations", **parameters["spike_locations"], n_jobs=1, progress_bar=False)
    except TypeError as error:
        assert "list indices" in str(error)
    else:
        raise AssertionError("Pinned library unexpectedly accepts a JSON list without conversion")
    analyzer.compute("spike_locations", **converted["spike_locations"], n_jobs=1, progress_bar=False)
    locations = analyzer.get_extension("spike_locations").get_data()
    assert len(locations) == 6
    assert all(np.isfinite(locations[name]).all() for name in locations.dtype.names)
    assert json.dumps(parameters, sort_keys=True) == before

    from spikeinterface.core.node_pipeline import SpikeRetriever
    from spikeinterface.core.template_tools import get_template_extremum_channel
    from spikeinterface.sortingcomponents.peak_localization import get_localization_pipeline_nodes

    retriever = SpikeRetriever(sorting, recording, extremum_channel_inds=get_template_extremum_channel(analyzer, outputs="index"))
    prototypes = []
    for options in ({}, {"prototype": None}):
        defaults = {"spike_locations": {"method": "grid_convolution", "method_kwargs": options}}
        normalized = analyzer_compute_parameters(defaults, 30000)
        nodes = [get_localization_pipeline_nodes(recording, retriever, method="grid_convolution",
                                                method_kwargs=value["spike_locations"]["method_kwargs"],
                                                job_kwargs={"n_jobs": 1, "progress_bar": False})
                 for value in (defaults, normalized)]
        assert np.array_equal(nodes[0][-1].prototype, nodes[1][-1].prototype)
        prototypes.append(nodes[1][-1].prototype)
    assert not np.array_equal(*prototypes), "Missing and explicit-null prototypes were collapsed"
    result = {"passed": True, "json_provenance_unchanged": True,
              "default_and_null_cases": len(unchanged), "rejected_cases": list(invalid),
              "integer_vectors_accepted": True, "pinned_api_list_failure_reproduced": True,
              "real_kernel_locations": len(locations), "prototype_dtype": str(actual.dtype),
              "default_sampled_and_null_gaussian_api_behaviors_preserved": True}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
