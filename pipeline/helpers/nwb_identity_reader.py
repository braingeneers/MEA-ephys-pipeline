"""Bound transient HDF5 selections made while fingerprinting NWB recordings.

Only the in-memory identity reader is adapted. Capsule readers, serialized graphs,
frame boundaries and preprocessing calculations retain their original behavior.
"""
from contextlib import contextmanager


SOURCE_BUFFER_BYTES = 8 * 1024 * 1024


class ContiguousChannelReads:
    """Serve valid indexed columns using byte-bounded contiguous HDF5 reads.

    HDF5 fancy selection can allocate gigabytes of selection metadata for a small
    signal block. Contiguous channel runs avoid that allocation. The returned
    array still spans the caller's complete frame range, so upstream filters see
    exactly the same inputs and margins.
    """

    def __init__(self, dataset, max_buffer_bytes=SOURCE_BUFFER_BYTES):
        if type(max_buffer_bytes) is not int or max_buffer_bytes <= 0:
            raise ValueError("Source buffer budget must be a positive integer")
        self._dataset = dataset
        self._max_buffer_bytes = max_buffer_bytes

    def __getattr__(self, name):
        return getattr(self._dataset, name)

    def __getitem__(self, key):
        import numpy as np

        dataset = self._dataset
        if not (dataset.ndim == 2 and dataset.dtype.kind in "biufc"
                and isinstance(key, tuple) and len(key) == 2
                and isinstance(key[0], slice) and key[0].step in (None, 1)
                and isinstance(key[1], np.ndarray) and key[1].ndim == 1
                and key[1].dtype.kind in "iu" and key[1].size
                and dataset.dtype.itemsize <= self._max_buffer_bytes):
            return dataset[key]
        rows, columns = key
        # Preserve h5py's errors for duplicates, unsorted or out-of-range indices.
        if (np.any(columns < 0) or np.any(columns >= dataset.shape[1])
                or np.any(columns[1:] <= columns[:-1])):
            return dataset[key]
        start, stop, _ = rows.indices(dataset.shape[0])
        if stop <= start:
            return dataset[key]
        columns = columns.astype("int64", copy=False)
        output = np.empty((stop - start, len(columns)), dtype=dataset.dtype)
        boundaries = [0, *(np.flatnonzero(columns[1:] != columns[:-1] + 1) + 1), len(columns)]
        max_columns = self._max_buffer_bytes // dataset.dtype.itemsize
        for left, right in zip(boundaries, boundaries[1:]):
            for first in range(left, right, max_columns):
                last = min(right, first + max_columns)
                source_first = int(columns[first])
                source_last = int(columns[last - 1]) + 1
                frames = max(1, self._max_buffer_bytes // ((last - first) * dataset.dtype.itemsize))
                for begin in range(start, stop, frames):
                    end = min(stop, begin + frames)
                    output[begin - start:end - start, first:last] = dataset[
                        begin:end, source_first:source_last]
        return output


@contextmanager
def bounded_nwb_reads(recording, max_buffer_bytes=SOURCE_BUFFER_BYTES):
    """Adapt only loaded NWB/HDF5 leaves, restoring them even on failure."""
    import h5py
    import spikeinterface as si
    from spikeinterface.extractors.nwbextractors import NwbRecordingSegment

    replacements, seen, pending = [], set(), [recording]
    try:
        while pending:
            value = pending.pop()
            if id(value) in seen:
                continue
            seen.add(id(value))
            if isinstance(value, si.BaseRecording):
                pending.extend((value.get_parent(), value._kwargs))
                for segment in value._recording_segments:
                    if isinstance(segment, NwbRecordingSegment):
                        dataset = segment.electrical_series_data
                        if isinstance(dataset, h5py.Dataset):
                            view = ContiguousChannelReads(dataset, max_buffer_bytes)
                            replacements.append((segment, dataset))
                            segment.electrical_series_data = view
            elif isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, (list, tuple)):
                pending.extend(value)
        yield
    finally:
        for segment, dataset in reversed(replacements):
            segment.electrical_series_data = dataset
