#!/usr/bin/env python3
"""Validate Maxwell-to-NWB workflow outputs against an original Maxwell recording."""

from __future__ import annotations

from workflow_defaults import scientific_default

import argparse
import json
import sys
from pathlib import Path

import h5py
import numpy as np
from pynwb import NWBHDF5IO

from maxwell_h5_utils import (
    configure_bundled_maxwell_hdf5_plugin,
    discover_routed_raw_dataset_path,
)
from generate_lindi_metadata import validate_maxwell_filter_masks
from maxwell_lindi_codec import (
    MAXWELL_PLUGIN_FILENAME,
    MaxwellMxwDataCodec,
    codec_config,
)

DEFAULT_NWB_DATASET_PATH = "/acquisition/ElectricalSeries/data"
def fail(message: str) -> None:
    print(f"[validate_conversion_outputs] {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-file", required=True, help="Path to the original Maxwell .raw.h5 file.")
    parser.add_argument("--nwb-file", required=True, help="Path to the generated NWB file.")
    parser.add_argument(
        "--linked-maxwell-file",
        required=True,
        help="Path to the generated compact Maxwell .linked.raw.h5 file.",
    )
    parser.add_argument("--lindi-file", required=True, help="Path to the generated .nwb.lindi.json file.")
    parser.add_argument(
        "--chunk-size-samples",
        type=int,
        default=50000,
        help="Time samples to compare at once for the full NWB data equality check.",
    )
    parser.add_argument(
        "--legacy-window-samples",
        type=int,
        default=2000,
        help="Time samples per representative legacy Maxwell read window.",
    )
    parser.add_argument(
        "--maxwell-raw-dataset-path",
        default=scientific_default('maxwell_raw_dataset_path'),
        help="Dataset path for the Maxwell raw table, or 'auto' to discover it.",
    )
    parser.add_argument(
        "--nwb-dataset-path",
        default=DEFAULT_NWB_DATASET_PATH,
        help="Dataset path for NWB ElectricalSeries/data.",
    )
    return parser.parse_args()


def prepare_maxwell_plugin() -> str:
    return str(configure_bundled_maxwell_hdf5_plugin())


def validate_nwb_data(
    *,
    original_file: Path,
    nwb_path: Path,
    raw_dataset_path: str,
    nwb_dataset_path: str,
    chunk_size_samples: int,
) -> tuple[tuple[int, int], tuple[int, int]]:
    with h5py.File(original_file, "r") as original_h5:
        if raw_dataset_path not in original_h5:
            fail(f"Original Maxwell raw dataset path not found: {raw_dataset_path}")
        raw_dataset = original_h5[raw_dataset_path]
        expected_channels = int(raw_dataset.shape[0])
        expected_frames = int(raw_dataset.shape[1])

    with h5py.File(nwb_path, "r") as h5f:
        if nwb_dataset_path not in h5f:
            fail(f"NWB dataset path not found: {nwb_dataset_path}")
        dataset = h5f[nwb_dataset_path]
        actual_shape = tuple(int(x) for x in dataset.shape)
        actual_chunks = tuple(int(x) for x in dataset.chunks)
        actual_dtype = dataset.dtype

    if actual_shape != (expected_frames, expected_channels):
        fail(
            "NWB ElectricalSeries/data shape mismatch: "
            f"expected={(expected_frames, expected_channels)}, actual={actual_shape}"
        )

    with NWBHDF5IO(str(nwb_path), "r", load_namespaces=True) as io:
        nwbfile = io.read()
        electrical_series = nwbfile.acquisition["ElectricalSeries"]
        nwb_data = electrical_series.data

        with h5py.File(original_file, "r") as original_h5:
            raw_dataset = original_h5[raw_dataset_path]
            for chunk_index, start in enumerate(range(0, expected_frames, chunk_size_samples), start=1):
                end = min(expected_frames, start + chunk_size_samples)
                original_chunk = np.asarray(raw_dataset[:, start:end]).T
                nwb_chunk = np.asarray(nwb_data[start:end, :])

                if nwb_chunk.dtype != actual_dtype:
                    fail(
                        "NWB chunk dtype mismatch while reading data: "
                        f"metadata_dtype={actual_dtype}, read_dtype={nwb_chunk.dtype}"
                    )

                if original_chunk.shape != nwb_chunk.shape:
                    fail(
                        "NWB chunk shape mismatch: "
                        f"original={original_chunk.shape}, nwb={nwb_chunk.shape}, start={start}, end={end}"
                    )

                if not np.array_equal(original_chunk, nwb_chunk):
                    mismatches = np.argwhere(original_chunk != nwb_chunk)
                    first_row, first_col = mismatches[0].tolist()
                    fail(
                        "NWB ephys data mismatch: "
                        f"frame={start + first_row}, channel_index={first_col}, "
                        f"original={original_chunk[first_row, first_col]}, "
                        f"nwb={nwb_chunk[first_row, first_col]}"
                    )

                if chunk_index == 1 or chunk_index % 10 == 0 or end == expected_frames:
                    print(
                        "[validate_conversion_outputs] NWB data chunk matched "
                        f"{start}:{end} of {expected_frames}",
                        flush=True,
                    )

    return actual_shape, actual_chunks


def validate_linked_maxwell(
    *,
    original_file: Path,
    linked_path: Path,
    nwb_path: Path,
    raw_dataset_path: str,
    nwb_dataset_path: str,
    legacy_window_samples: int,
) -> None:
    with h5py.File(original_file, "r") as original_h5, h5py.File(linked_path, "r") as linked_h5:
        if raw_dataset_path not in original_h5:
            fail(f"Original Maxwell raw dataset path not found: {raw_dataset_path}")
        if raw_dataset_path not in linked_h5:
            fail(f"Linked Maxwell raw dataset path not found: {raw_dataset_path}")
        original_raw_dataset = original_h5[raw_dataset_path]
        raw_dataset = linked_h5[raw_dataset_path]
        if not raw_dataset.is_virtual:
            fail("Linked Maxwell raw dataset is not an HDF5 virtual dataset.")
        if raw_dataset.attrs.get("legacy_data_bridge_target_file") != nwb_path.name:
            fail(
                "Linked Maxwell bridge target file does not match the provided NWB file basename: "
                f"expected={nwb_path.name}, "
                f"actual={raw_dataset.attrs.get('legacy_data_bridge_target_file')}"
            )
        if raw_dataset.attrs.get("legacy_data_bridge_target_dataset") != nwb_dataset_path:
            fail(
                "Linked Maxwell bridge target dataset path mismatch: "
                f"expected={nwb_dataset_path}, "
                f"actual={raw_dataset.attrs.get('legacy_data_bridge_target_dataset')}"
            )

        if tuple(raw_dataset.shape) != tuple(original_raw_dataset.shape):
            fail(
                "Linked Maxwell raw dataset shape mismatch: "
                f"expected={original_raw_dataset.shape}, actual={raw_dataset.shape}"
            )
        if raw_dataset.dtype != original_raw_dataset.dtype:
            fail(
                "Linked Maxwell raw dataset dtype mismatch: "
                f"expected={original_raw_dataset.dtype}, actual={raw_dataset.dtype}"
            )

        direct_slice = raw_dataset[:5, :20]
        original_slice = original_raw_dataset[:5, :20]
        if not np.array_equal(original_slice, direct_slice):
            fail("Direct h5py reads from the linked Maxwell raw dataset do not match the original raw table.")

        total_frames = int(original_raw_dataset.shape[1])
        window = min(legacy_window_samples, total_frames)
        starts = sorted({0, max(0, (total_frames // 2) - (window // 2)), max(0, total_frames - window)})

        for start in starts:
            end = min(total_frames, start + window)
            original_chunk = original_raw_dataset[:, start:end]
            linked_chunk = raw_dataset[:, start:end]
            if not np.array_equal(original_chunk, linked_chunk):
                fail(
                    "Legacy Maxwell raw-table read mismatch for linked companion: "
                    f"start={start}, end={end}"
                )
            print(
                "[validate_conversion_outputs] Linked Maxwell raw-table read matched "
                f"{start}:{end} of {total_frames}",
                flush=True,
            )

        if raw_dataset.attrs.get("legacy_data_bridge") != "virtual_dataset_to_nwb":
            fail(
                "Linked Maxwell raw dataset does not advertise the expected bridge marker: "
                f"actual={raw_dataset.attrs.get('legacy_data_bridge')}"
            )


def validate_lindi(
    *,
    lindi_path: Path,
    nwb_path: Path,
    expected_shape: tuple[int, int],
    expected_chunks: tuple[int, int],
    nwb_dataset_path: str,
    plugin_directory: Path,
) -> None:
    with open(lindi_path, "r", encoding="utf-8") as f:
        lindi = json.load(f)

    for key in ("generationMetadata", "refs", "templates", "version"):
        if key not in lindi:
            fail(f"LINDI metadata is missing top-level key: {key}")

    generation_metadata = lindi["generationMetadata"]
    for key in (
        "sourceNwbFile",
        "sourceUrl",
        "numDatasetChunksThreshold",
        "contiguousDatasetMaxChunkSize",
        "maxwellFilter401FallbackApplied",
        "maxwellFilter401Codec",
    ):
        if key not in generation_metadata:
            fail(f"LINDI generationMetadata is missing key: {key}")

    if generation_metadata["maxwellFilter401FallbackApplied"] is not False:
        fail(
            "LINDI generationMetadata reports a filter-401 fallback. "
            "Filter 401 must be represented explicitly."
        )
    if generation_metadata["maxwellFilter401Codec"] != codec_config():
        fail(
            "LINDI generationMetadata has an unexpected Maxwell codec declaration: "
            f"expected={codec_config()}, "
            f"actual={generation_metadata['maxwellFilter401Codec']}"
        )

    source_url = str(generation_metadata["sourceUrl"])
    if not source_url.endswith(nwb_path.name):
        fail(
            "LINDI sourceUrl does not end with the NWB filename: "
            f"sourceUrl={source_url}, nwb_name={nwb_path.name}"
        )

    refs = lindi["refs"]
    dataset_prefix = nwb_dataset_path.lstrip("/")
    zarray_key = f"{dataset_prefix}/.zarray"
    zattrs_key = f"{dataset_prefix}/.zattrs"

    if zarray_key not in refs:
        fail(f"LINDI refs is missing {zarray_key}")
    if zattrs_key not in refs:
        fail(f"LINDI refs is missing {zattrs_key}")

    zarray = refs[zarray_key]
    if tuple(zarray.get("shape", ())) != expected_shape:
        fail(f"LINDI .zarray shape mismatch: expected={expected_shape}, actual={zarray.get('shape')}")
    if tuple(zarray.get("chunks", ())) != expected_chunks:
        fail(f"LINDI .zarray chunks mismatch: expected={expected_chunks}, actual={zarray.get('chunks')}")
    if zarray.get("dtype") != "<u2":
        fail(f"LINDI .zarray dtype mismatch: expected=<u2, actual={zarray.get('dtype')}")
    if zarray.get("order") != "C":
        fail(f"LINDI .zarray order mismatch: expected=C, actual={zarray.get('order')}")
    if zarray.get("compressor") is not None:
        fail(
            "LINDI .zarray must represent HDF5 filter 401 in filters, not compressor: "
            f"actual={zarray.get('compressor')}"
        )
    if zarray.get("filters") != [codec_config()]:
        fail(
            "LINDI .zarray Maxwell filter declaration mismatch: "
            f"expected={[codec_config()]}, actual={zarray.get('filters')}"
        )

    chunk_ref_count = sum(
        1
        for key in refs
        if key.startswith(f"{dataset_prefix}/")
        and not key.endswith(".zarray")
        and not key.endswith(".zattrs")
    )
    if chunk_ref_count == 0:
        fail("LINDI refs does not contain explicit chunk references for ElectricalSeries/data.")

    filter_dataset_count, filter_chunk_count = validate_maxwell_filter_masks(nwb_path)
    decoder = MaxwellMxwDataCodec(
        plugin_path=plugin_directory / MAXWELL_PLUGIN_FILENAME
    )
    time_chunk_count = (expected_shape[0] + expected_chunks[0] - 1) // expected_chunks[0]
    channel_chunk_count = (expected_shape[1] + expected_chunks[1] - 1) // expected_chunks[1]
    time_chunk_indices = sorted({0, time_chunk_count // 2, time_chunk_count - 1})
    channel_chunk_indices = sorted({0, channel_chunk_count // 2, channel_chunk_count - 1})
    decoded_chunk_count = 0

    with h5py.File(nwb_path, "r") as h5f, open(nwb_path, "rb") as nwb_stream:
        dataset = h5f[nwb_dataset_path]
        for time_chunk_index in time_chunk_indices:
            for channel_chunk_index in channel_chunk_indices:
                chunk_key = (
                    f"{dataset_prefix}/{time_chunk_index}.{channel_chunk_index}"
                )
                reference = refs.get(chunk_key)
                if (
                    not isinstance(reference, list)
                    or len(reference) != 3
                    or not isinstance(reference[1], int)
                    or not isinstance(reference[2], int)
                ):
                    fail(
                        "LINDI chunk reference is missing or malformed: "
                        f"key={chunk_key}, reference={reference}"
                    )

                byte_offset = reference[1]
                byte_length = reference[2]
                if byte_offset < 0 or byte_length <= 0:
                    fail(
                        "LINDI chunk reference has an invalid byte range: "
                        f"key={chunk_key}, offset={byte_offset}, length={byte_length}"
                    )
                nwb_stream.seek(byte_offset)
                compressed = nwb_stream.read(byte_length)
                if len(compressed) != byte_length:
                    fail(
                        "LINDI chunk reference extends beyond the NWB file: "
                        f"key={chunk_key}, expected_bytes={byte_length}, "
                        f"read_bytes={len(compressed)}"
                    )

                decoded = decoder.decode(compressed)
                expected_decoded_bytes = int(np.prod(expected_chunks)) * np.dtype("<u2").itemsize
                if len(decoded) != expected_decoded_bytes:
                    fail(
                        "Maxwell filter-401 decoded chunk size mismatch: "
                        f"key={chunk_key}, expected={expected_decoded_bytes}, "
                        f"actual={len(decoded)}"
                    )

                decoded_array = np.frombuffer(decoded, dtype="<u2").reshape(
                    expected_chunks,
                    order="C",
                )
                time_start = time_chunk_index * expected_chunks[0]
                channel_start = channel_chunk_index * expected_chunks[1]
                time_end = min(time_start + expected_chunks[0], expected_shape[0])
                channel_end = min(
                    channel_start + expected_chunks[1],
                    expected_shape[1],
                )
                expected_array = np.zeros(expected_chunks, dtype="<u2")
                expected_array[
                    : time_end - time_start,
                    : channel_end - channel_start,
                ] = dataset[time_start:time_end, channel_start:channel_end]
                if not np.array_equal(decoded_array, expected_array):
                    mismatches = np.argwhere(decoded_array != expected_array)
                    first_row, first_column = mismatches[0].tolist()
                    fail(
                        "Maxwell filter-401 native decode differs from direct HDF5 data: "
                        f"key={chunk_key}, chunk_index=({first_row}, {first_column}), "
                        f"decoded={decoded_array[first_row, first_column]}, "
                        f"direct={expected_array[first_row, first_column]}"
                    )
                decoded_chunk_count += 1

    print(
        "[validate_conversion_outputs] LINDI metadata validated "
        f"(chunk_ref_count={chunk_ref_count}, "
        f"filter401_datasets={filter_dataset_count}, "
        f"filter401_chunks={filter_chunk_count}, "
        f"native_decoded_chunks={decoded_chunk_count})",
        flush=True,
    )


def main() -> None:
    args = parse_args()

    original_file = Path(args.original_file).resolve()
    nwb_file = Path(args.nwb_file).resolve()
    linked_maxwell_file = Path(args.linked_maxwell_file).resolve()
    lindi_file = Path(args.lindi_file).resolve()

    for path, label in (
        (original_file, "original Maxwell file"),
        (nwb_file, "NWB file"),
        (linked_maxwell_file, "linked Maxwell file"),
        (lindi_file, "LINDI file"),
    ):
        if not path.exists():
            fail(f"Missing {label}: {path}")

    if args.chunk_size_samples <= 0:
        fail("--chunk-size-samples must be > 0")
    if args.legacy_window_samples <= 0:
        fail("--legacy-window-samples must be > 0")

    try:
        raw_dataset_path = discover_routed_raw_dataset_path(
            original_file,
            requested_path=args.maxwell_raw_dataset_path,
            require_single=args.maxwell_raw_dataset_path.lower() == "auto",
        )
    except Exception as exc:
        fail(str(exc))

    plugin_path = prepare_maxwell_plugin()
    print(f"[validate_conversion_outputs] Using Maxwell HDF5 plugin path: {plugin_path}", flush=True)

    expected_shape, expected_chunks = validate_nwb_data(
        original_file=original_file,
        nwb_path=nwb_file,
        raw_dataset_path=raw_dataset_path,
        nwb_dataset_path=args.nwb_dataset_path,
        chunk_size_samples=args.chunk_size_samples,
    )
    validate_linked_maxwell(
        original_file=original_file,
        linked_path=linked_maxwell_file,
        nwb_path=nwb_file,
        raw_dataset_path=raw_dataset_path,
        nwb_dataset_path=args.nwb_dataset_path,
        legacy_window_samples=args.legacy_window_samples,
    )
    validate_lindi(
        lindi_path=lindi_file,
        nwb_path=nwb_file,
        expected_shape=expected_shape,
        expected_chunks=expected_chunks,
        nwb_dataset_path=args.nwb_dataset_path,
        plugin_directory=Path(plugin_path),
    )

    print("[validate_conversion_outputs] All validations passed.", flush=True)


if __name__ == "__main__":
    main()
