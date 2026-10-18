"""Kilosort-owned terminal outcome recognition, shared with stage validation."""
import re


def detected_no_spikes(sorter_log):
    """Recognize only Kilosort's completed, zero-spike detection outcome."""
    error_trace = sorter_log.get("error_trace", [])
    runtime_trace = sorter_log.get("runtime_trace", [])
    return (
        sorter_log.get("sorter_name") == "kilosort4"
        and sorter_log.get("error") is True
        and bool(error_trace)
        and error_trace[-1].strip() == "ValueError: No spikes detected, cannot continue sorting."
        and any(re.search(r"\b0 spikes extracted in ", line) for line in runtime_trace)
        and any("st0 shape: (0, 6)" in line for line in runtime_trace)
    )
