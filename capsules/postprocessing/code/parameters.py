"""Convert explicit JSON array parameters at the scientific API boundary."""
from copy import deepcopy
from numbers import Real

import numpy as np


def analyzer_compute_parameters(parameters, sampling_frequency):
    """Keep JSON provenance intact while supplying SI's optional prototype array.

    Missing prototypes retain SI's sampled default. Explicit null retains its
    Gaussian fallback. JSON vectors use float32, matching the pinned sampled
    prototype's dtype; no other scientific setting is changed.
    """
    location = parameters.get("spike_locations", {})
    method = location.get("method", "center_of_mass")
    options = location.get("method_kwargs") or {}
    if method != "grid_convolution" or options.get("prototype") is None:
        return parameters

    field = "postprocessing.spike_locations.method_kwargs.prototype"
    value = options["prototype"]
    try:
        array = np.asarray(value)
    except (ValueError, TypeError) as error:
        raise ValueError(f"{field} must be a finite one-dimensional numeric vector") from error
    if array.ndim != 1 or not len(array):
        raise ValueError(f"{field} must be a nonempty one-dimensional vector")
    if any(isinstance(item, (bool, np.bool_)) or not isinstance(item, Real) for item in value):
        raise ValueError(f"{field} must contain numbers, not booleans or strings")
    with np.errstate(over="ignore", invalid="ignore"):
        array = np.asarray(value, dtype=np.float32)
    if not np.isfinite(array).all():
        raise ValueError(f"{field} must contain finite float32 values")

    before = location.get("ms_before", 0.5)
    after = location.get("ms_after", 0.5)
    expected = int(before * sampling_frequency / 1000) + int(after * sampling_frequency / 1000)
    if len(array) != expected:
        raise ValueError(f"{field} needs {expected} samples for the recording rate and spike-location window; got {len(array)}")

    result = dict(parameters)
    result["spike_locations"] = deepcopy(location)
    result["spike_locations"]["method_kwargs"]["prototype"] = array
    return result
