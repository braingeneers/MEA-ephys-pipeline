"""Bind reported trains and complete decoded report/burst scientific fields.

Receipts are created by assembly, after its analyzer checks. Verification only
compares existing expectations; it never upgrades historical bundles.
"""
import hashlib
import json
import math

import portable_bundle as portable


def require(condition, message):
    if not condition:
        raise portable.BundleError(message)


def unit_identity(value, recording):
    import numpy as np

    if isinstance(value, np.generic):
        value = value.item()
    if type(value) is int:
        return ("integer", str(value))
    if type(value) is str:
        return ("string", value)
    raise portable.BundleError(f"Invalid report unit identity for {recording}")


def bind_trains(times, analyzer, recording):
    """Use the report producer's own sorting and clock, with separate selection."""
    import numpy as np

    require(isinstance(times, dict), f"Invalid report spike dictionary for {recording}")
    require(analyzer.sorting.get_num_segments() == 1,
            f"Report requires a single sorting segment for {recording}")
    units = {unit_identity(unit, recording): unit for unit in analyzer.unit_ids}
    for key, values in times.items():
        identity = unit_identity(key, recording)
        require(identity in units, f"Report spike unit differs from analyzer for {recording}")
        expected = analyzer.sorting.get_unit_spike_train(units[identity], segment_index=0)
        expected = expected / analyzer.recording.get_sampling_frequency()
        require(isinstance(values, np.ndarray) and values.dtype == expected.dtype
                and values.shape == expected.shape and np.isfinite(values).all()
                and values.tobytes(order="C") == expected.tobytes(order="C"),
                f"Report spike train differs from analyzer for {recording}: {key!r}")


def validate_bursts(bursts, times, recording):
    """The capsule removes empty trains before counting units and making events."""
    count = sum(bool(len(values)) for values in times.values())
    require(isinstance(bursts, dict) and type(bursts.get("n_units")) is int
            and bursts["n_units"] == count
            and bursts.get("status") == ("ok" if count else "no_spikes"),
            f"Burst outcome differs from report for {recording}")
    for family in ("burstlets", "network_bursts", "superbursts"):
        value = bursts.get(family)
        require(isinstance(value, dict) and isinstance(value.get("events"), list),
                f"Malformed burst events for {recording}: {family}")
        if not count:
            require(not value["events"], f"Nonempty burst events without spikes for {recording}")


def typed_json(value):
    """Complete JSON values with numeric types and binary64 signs preserved."""
    if isinstance(value, dict):
        require(all(type(key) is str for key in value), "Scientific JSON keys must be strings")
        return ["object", [[key, typed_json(value[key])] for key in sorted(value)]]
    if isinstance(value, list):
        return ["array", [typed_json(item) for item in value]]
    if type(value) is float:
        require(math.isfinite(value), "Scientific report JSON contains a nonfinite number")
        return ["float64", value.hex()]
    if type(value) is int:
        return ["integer", str(value)]
    if value is None or type(value) in (str, bool):
        return [type(value).__name__, value]
    raise portable.BundleError("Unsupported scientific report JSON value")


def field_identity(value):
    encoded = json.dumps(typed_json(value), separators=(",", ":"), allow_nan=False).encode()
    return {"algorithm": "mea.typed-json/v1", "sha256": hashlib.sha256(encoded).hexdigest()}


def document(path, recording):
    value = json.loads(path.read_text())
    require(isinstance(value, dict), f"Malformed scientific report document for {recording}: {path.name}")
    return {key: field_identity(item) for key, item in sorted(value.items())}


def capture(data, recording_ids):
    """Fingerprint full decoded science by field, independently of file encoding."""
    import numpy as np

    rows = []
    for recording in recording_ids:
        folder = data / "reports" / recording
        times = np.load(folder / "spike_times.npy", allow_pickle=True).item()
        require(isinstance(times, dict), f"Invalid report spike dictionary for {recording}")
        units = []
        for key, values in times.items():
            require(isinstance(values, np.ndarray) and values.ndim == 1
                    and values.dtype.kind == "f" and np.isfinite(values).all(),
                    f"Invalid reported spike times for {recording}")
            units.append({"unit": list(unit_identity(key, recording)),
                          "key_type": type(key).__module__ + "." + type(key).__qualname__,
                          "key_dtype": np.asarray(key).dtype.str,
                          "train": portable.array_identity(values)})
        rows.append({"recording_id": recording, "unit_trains": units,
                     "summary": document(folder / "report_summary.json", recording),
                     "bursts": document(data / "bursts" / recording / "network_results.json", recording)})
    return {"schema_version": 1, "recordings": rows}


def verify(data, record):
    expected = record.get("metadata", {}).get("reported_science")
    require(isinstance(expected, dict) and type(expected.get("schema_version")) is int
            and expected["schema_version"] == 1 and set(expected) == {"schema_version", "recordings"}
            and isinstance(expected["recordings"], list), "Missing or malformed reported scientific receipt")
    actual = capture(data, record["recording_ids"])
    require(len(expected["recordings"]) == len(actual["recordings"]),
            "Reported scientific recording inventory differs from receipt")
    for wanted, observed in zip(expected["recordings"], actual["recordings"]):
        recording = observed["recording_id"]
        require(isinstance(wanted, dict) and set(wanted) == set(observed)
                and wanted["recording_id"] == recording, "Malformed reported scientific recording receipt")
        require(portable.encoded(wanted["unit_trains"]) == portable.encoded(observed["unit_trains"]),
                f"Report unit/train scientific receipt differs for {recording}")
        for role, label in (("summary", "Report summary"), ("bursts", "Burst")):
            require(isinstance(wanted[role], dict) and set(wanted[role]) == set(observed[role]),
                    f"{label} scientific field inventory differs from receipt for {recording}")
            for field, value in observed[role].items():
                require(portable.encoded(wanted[role][field]) == portable.encoded(value),
                        f"{label} scientific field differs from receipt for {recording}: {field}")
