#!/usr/bin/env python3
"""NumCodecs declaration and native decoder for Maxwell HDF5 filter 401."""

from __future__ import annotations

import ctypes
import os
import sys
from functools import cache
from pathlib import Path

import numpy as np
from numcodecs.abc import Codec
from numcodecs.registry import register_codec


MAXWELL_FILTER_ID = 401
MAXWELL_CODEC_ID = "maxwell-mxw-data"
MAXWELL_CODEC_VERSION = 1
MAXWELL_PLUGIN_FILENAME = "libcompression.so"


def codec_config() -> dict[str, object]:
    """Return the public Zarr filter configuration for Maxwell filter 401."""
    return {
        "id": MAXWELL_CODEC_ID,
        "hdf5_filter_id": MAXWELL_FILTER_ID,
        "codec_version": MAXWELL_CODEC_VERSION,
    }


def _candidate_plugin_paths(explicit_path: str | Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if explicit_path:
        explicit_candidate = Path(explicit_path)
        if explicit_candidate.is_dir():
            explicit_candidate = explicit_candidate / MAXWELL_PLUGIN_FILENAME
        candidates.append(explicit_candidate)

    for raw_directory in os.environ.get("HDF5_PLUGIN_PATH", "").split(os.pathsep):
        if raw_directory:
            candidates.append(Path(raw_directory) / MAXWELL_PLUGIN_FILENAME)

    candidates.extend(
        [
            Path.home() / "hdf5_plugin_path_maxwell" / MAXWELL_PLUGIN_FILENAME,
            Path("/project/src") / MAXWELL_PLUGIN_FILENAME,
            Path("/usr/local/hdf5/lib/plugin") / MAXWELL_PLUGIN_FILENAME,
        ]
    )

    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def find_maxwell_plugin(explicit_path: str | Path | None = None) -> Path:
    """Find the native Maxwell filter library using the runtime plugin paths."""
    candidates = _candidate_plugin_paths(explicit_path)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    checked = ", ".join(str(path) for path in candidates) or "(no candidates)"
    raise RuntimeError(f"Maxwell filter 401 decoder is unavailable; checked: {checked}")


@cache
def _load_native_decoder(plugin_path: str) -> tuple[ctypes.CDLL, object, object]:
    library = ctypes.CDLL(plugin_path)
    decompress = library.decompress_samples
    decompress.argtypes = [
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint16)),
    ]
    decompress.restype = ctypes.c_size_t

    process_library = ctypes.CDLL(None)
    release = process_library.free
    release.argtypes = [ctypes.c_void_p]
    release.restype = None
    return library, decompress, release


class MaxwellMxwDataCodec(Codec):
    """Decode chunks compressed with Maxwell's ``mxw-data`` HDF5 filter."""

    codec_id = MAXWELL_CODEC_ID

    def __init__(
        self,
        hdf5_filter_id: int = MAXWELL_FILTER_ID,
        codec_version: int = MAXWELL_CODEC_VERSION,
        *,
        plugin_path: str | Path | None = None,
    ) -> None:
        if hdf5_filter_id != MAXWELL_FILTER_ID:
            raise ValueError(
                f"{MAXWELL_CODEC_ID} requires hdf5_filter_id={MAXWELL_FILTER_ID}, "
                f"got {hdf5_filter_id}"
            )
        if codec_version != MAXWELL_CODEC_VERSION:
            raise ValueError(
                f"{MAXWELL_CODEC_ID} requires codec_version={MAXWELL_CODEC_VERSION}, "
                f"got {codec_version}"
            )
        self.hdf5_filter_id = hdf5_filter_id
        self.codec_version = codec_version
        self.plugin_path = str(plugin_path) if plugin_path else None

    def get_config(self) -> dict[str, object]:
        return codec_config()

    def encode(self, buf):  # pragma: no cover - the workflow uses HDF5 for encoding
        raise NotImplementedError(f"{MAXWELL_CODEC_ID} encoding is not implemented")

    def decode(self, buf, out=None):
        compressed = memoryview(buf).cast("B").tobytes()
        if not compressed:
            raise ValueError(f"{MAXWELL_CODEC_ID} cannot decode an empty chunk")

        plugin_path = find_maxwell_plugin(self.plugin_path)
        _library, decompress, release = _load_native_decoder(str(plugin_path))
        input_buffer = (ctypes.c_uint8 * len(compressed)).from_buffer_copy(compressed)
        output_pointer = ctypes.POINTER(ctypes.c_uint16)()
        sample_count = int(
            decompress(input_buffer, len(compressed), ctypes.byref(output_pointer))
        )
        if not output_pointer or sample_count <= 0:
            raise RuntimeError(
                f"{MAXWELL_CODEC_ID} returned an invalid decoded sample count: {sample_count}"
            )

        try:
            decoded_array = np.ctypeslib.as_array(
                output_pointer,
                shape=(sample_count,),
            ).copy()
        finally:
            release(output_pointer)

        if sys.byteorder != "little":
            decoded_array = decoded_array.astype("<u2", copy=False)
        decoded = decoded_array.tobytes(order="C")
        if out is None:
            return decoded

        output_view = memoryview(out).cast("B")
        if len(output_view) != len(decoded):
            raise ValueError(
                f"{MAXWELL_CODEC_ID} decoded {len(decoded)} bytes into an "
                f"{len(output_view)}-byte output buffer"
            )
        output_view[:] = decoded
        return out


def register_maxwell_mxw_data_codec() -> None:
    """Register the Maxwell decoder explicitly with NumCodecs."""
    register_codec(MaxwellMxwDataCodec)
