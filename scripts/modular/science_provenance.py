"""Strict execution-field separation for the single-recording science check.

Only known serializer paths, ISO execution dates and Kilosort resource/duration
fields are separated. Unknown fields and scientific log text remain compared.
Full recording/spike payload checks are required alongside these representations.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re

import numpy as np


def sorter_log(check, label, value):
    result = deepcopy(value)
    assert set(result) == {"sorter_name", "sorter_version", "datetime", "runtime_trace", "error", "run_time"}
    check.execution_time(label + ".datetime", result.pop("datetime"))
    assert result.pop("run_time") >= 0
    check.execution_fields.append(label + ".run_time")
    assert result["error"] is False, "Reference sorter did not succeed"
    lines = []
    for index, line in enumerate(result["runtime_trace"]):
        stamp = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) (.*)$", line)
        assert stamp, line
        datetime.strptime(stamp[1], "%Y-%m-%d %H:%M:%S,%f")
        text = stamp[2]
        resource = re.fullmatch(
            r"(kilosort\.[\w.]+\s+(?:DEBUG|INFO)\s+)"
            r"((?:CPU usage|Mem used|Mem avail|GPU usage|GPU memory):)\s+(.+)", text
        )
        if resource:
            assert re.fullmatch(r"N/A|[\d. %|/GB]+", resource[3]), text
            text = resource[1] + resource[2] + " <execution resource>"
        elif "Total runtime:" in text:
            # The pinned producer uses :02d for hours/minutes, then
            # round(seconds) without padding (which can also round up to 60).
            text, count = re.subn(r"Total runtime: \d+(?:\.\d+)?s = \d{2,}:[0-5]\d:(?:[0-5]?\d|60) h:m:s$",
                                 "Total runtime: <execution duration>", text)
            assert count == 1, text
        elif "Sorting output saved in:" in text:
            prefix, folder = text.split("Sorting output saved in: ", 1)
            path = Path(folder.rstrip("."))
            assert path.name == "sorter_output"
            text = prefix + "Sorting output saved in: <execution folder>/" + path.parent.name + "/sorter_output."
        else:
            # Preserve all counts/shapes/values. Only the named measured
            # durations in the pinned producer's text are replaced.
            text = re.sub(r"(?<=in )\d+(?:\.\d+)?s(?=;|$)", "<duration>s", text)
            text = re.sub(r"(?<=total )\d+(?:\.\d+)?s$", "<duration>s", text)
            text = re.sub(r"(?<=took: )\d+(?:\.\d+)?s$", "<duration>s", text)
        lines.append(text)
        if text != line:
            check.execution_fields.append(f"{label}.runtime_trace[{index}] (timestamp/resource/duration/path)")
    assert any("units found with good refractory periods" in line for line in lines)
    assert any("Exporting to Phy took:" in line for line in lines)
    result["runtime_trace"] = lines
    return result


def canonical(check, label, value):
    """Retain serializer graphs while separating their relocation coordinates."""
    if isinstance(value, np.ndarray):
        if value.dtype.hasobject:
            result = value.copy()
            for index in np.ndindex(value.shape):
                result[index] = canonical(check, label + str(index), value[index])
            return result
        return value
    if isinstance(value, (list, tuple)):
        return [canonical(check, f"{label}[{index}]", item) for index, item in enumerate(value)]
    if not isinstance(value, dict):
        return value
    if "runtime_trace" in value and "sorter_name" in value:
        return sorter_log(check, label, value)
    result = {}
    serialized = {"class", "module", "kwargs"} <= value.keys()
    for key, item in value.items():
        name = label + "." + str(key)
        if serialized and key == "relative_paths":
            assert isinstance(item, bool)
            check.execution_fields.append(name + " (serialization mode; graph and data compared)")
        elif key == "phy_folder":
            path = Path(item)
            assert path.name == "sorter_output"
            result[key] = path.parent.name + "/sorter_output"
            check.execution_fields.append(name + " (historical sorter folder)")
        elif key == "data_base_folder":
            assert isinstance(item, str) and item
            result[key] = "<serialization base>"
            check.execution_fields.append(name + " (declared base; complete recordings checked)")
        elif key in ("start_date_time", "end_date_time") and value.get("object_type") == "Data process":
            check.execution_time(name, item)
        elif serialized and key == "kwargs":
            kwargs = canonical(check, name, item)
            for field in ("folder_path", "file_path", "file_paths"):
                if field not in kwargs:
                    continue
                paths = kwargs[field] if field == "file_paths" else [kwargs[field]]
                assert all(isinstance(path, str) and path for path in paths)
                # Exact payloads, time/gain/geometry and source headers are
                # checked separately. Keep each referenced filename and every
                # other reader/preprocessing argument in this full graph.
                paths = [Path(path).name or "." for path in paths]
                kwargs[field] = paths if field == "file_paths" else paths[0]
                check.execution_fields.append(name + "." + field + " (serializer location; data checked)")
            result[key] = kwargs
        else:
            result[key] = canonical(check, name, item)
    return result
