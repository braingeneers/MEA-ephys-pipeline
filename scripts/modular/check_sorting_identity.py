#!/usr/bin/env python3
"""Check exact sorting identities with real SpikeInterface objects in the pinned image."""
import argparse
import importlib.util
import json
from pathlib import Path

import numpy as np
from spikeinterface.core import NumpySorting

SOURCE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("portable_bundle", SOURCE / "pipeline/helpers/portable_bundle.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists()
    dtype = [("sample_index", "int64"), ("unit_index", "int64"), ("segment_index", "int64")]
    spikes = np.array([(10, 0, 0), (10, 1, 0), (20, 1, 0), (20, 0, 0),
                       (5, 2, 1), (10, 0, 1), (10, 2, 1)], dtype=dtype)
    ids = np.array(["a", "b", "c"])

    def sorting(vector=spikes, rate=20000, units=ids):
        return NumpySorting(vector.copy(), rate, units.copy())

    original = sorting()
    before = original.to_spike_vector().tobytes()
    expected = bundle.sorting_identity(original)
    alternate = sorting(spikes[[1, 0, 3, 2, 4, 6, 5]])
    assert bundle.array_identity(original.to_spike_vector()) != bundle.array_identity(alternate.to_spike_vector())
    assert bundle.encoded(expected) == bundle.encoded(bundle.sorting_identity(alternate))
    assert original.to_spike_vector().tobytes() == before
    assert expected["spike_vector_order"] == "segment_sample_unit/v1"
    assert len(expected["spike_trains"]) == 2
    results = {"simultaneous_event_order": "equal exact identity", "source_vector_unchanged": True}

    def changed(label, candidate):
        assert bundle.encoded(bundle.sorting_identity(candidate)) != bundle.encoded(expected), label
        results[label] = "detected"

    candidate = spikes.copy(); candidate[0]["sample_index"] = 9
    changed("sample", sorting(candidate))
    candidate = spikes.copy(); candidate[2]["unit_index"] = 2
    changed("unit_assignment", sorting(candidate))
    changed("multiplicity", sorting(np.insert(spikes, 0, spikes[0])))
    changed("clock", sorting(rate=20001))
    changed("unit_identity", sorting(units=np.array(["a", "b", "other"])))
    changed("unit_order", sorting(units=ids[::-1]))
    candidate = spikes.copy(); candidate[-1]["segment_index"] = 2
    changed("segment", sorting(candidate))
    changed("vector_dtype", sorting(spikes.astype([(name, "int32") for name, _ in dtype])))
    # A changed per-unit view must be visible even if a stale cached vector survives.
    candidate = spikes.copy(); candidate[0]["sample_index"] = 9
    stale = sorting(candidate); stale._cached_spike_vector = spikes.copy()
    changed("ordered_train_independent_of_cached_vector", stale)
    for label, vector in [("nonchronological_vector", spikes[[2, 1, 0, 3, 4, 5, 6]]),
                          ("vector_shape", spikes[:, None])]:
        candidate = sorting(); candidate._cached_spike_vector = vector
        try:
            bundle.sorting_identity(candidate)
        except bundle.BundleError:
            results[label] = "rejected"
        else:
            raise AssertionError(label)
    # Genuine zero units retain a one-segment empty vector and its exact dtype.
    empty = sorting(np.zeros(0, dtype=dtype), units=np.array([], dtype="int64"))
    empty_identity = bundle.sorting_identity(empty)
    assert empty_identity["unit_ids"] == [] and empty_identity["spikes"]["shape"] == [0]
    assert empty_identity["spike_trains"] == [[]]
    results["zero_units"] = "preserved"
    value = dict(passed=True, numpy=np.__version__, cases=results, identity=expected,
                 scientific_capsules_changed=False, numerical_tolerance_added=False)
    args.output.write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps(value, indent=2))


if __name__ == "__main__":
    main()
