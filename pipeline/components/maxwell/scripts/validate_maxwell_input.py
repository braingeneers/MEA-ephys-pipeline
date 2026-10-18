#!/usr/bin/env python3
"""Validate that a staged Maxwell input is a supported single-recording Maxwell file."""

from __future__ import annotations

from workflow_defaults import scientific_default

import argparse
import sys
import importlib
from pathlib import Path

import h5py

from maxwell_h5_utils import (
    configure_bundled_maxwell_hdf5_plugin,
    count_routed_raw_dataset_paths,
    create_maxone_compatibility_view,
    discover_routed_raw_dataset_path,
    mapping_has_duplicate_channels,
    require_hdf5_filter_capabilities,
    well_has_other_recordings,
)


def fail(message: str) -> None:
    print(f"[validate_maxwell_input] {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", required=True, help="Path to staged Maxwell .raw.h5 file")
    parser.add_argument(
        "--maxwell-raw-dataset-path",
        default=scientific_default('maxwell_raw_dataset_path'),
        help="Raw dataset path or 'auto' to discover the single routed raw dataset.",
    )
    parser.add_argument(
        "--output-raw-dataset-path-file",
        default="raw_dataset_path.txt",
        help="Path to write the resolved raw dataset path.",
    )
    parser.add_argument(
        "--verify-all-raw-chunks",
        action="store_true",
        help="Read and decompress the complete routed raw dataset before accepting the input.",
    )
    return parser.parse_args()


def load_maxone_interface_class():
    candidates = [
        ("neuroconv.datainterfaces", "MaxOneRecordingInterface"),
        ("neuroconv.datainterfaces.ecephys", "MaxOneRecordingInterface"),
    ]

    for module_name, class_name in candidates:
        try:
            module = importlib.import_module(module_name)
            return getattr(module, class_name)
        except Exception:
            continue

    fail(
        "Could not import MaxOneRecordingInterface from NeuroConv. "
        "Verify the container includes NeuroConv with Maxwell support."
    )


def dataset_filter_ids(dataset: h5py.Dataset) -> list[int]:
    properties = dataset.id.get_create_plist()
    return [int(properties.get_filter(index)[0]) for index in range(properties.get_nfilters())]


def configure_maxwell_decoder() -> Path:
    plugin_path = configure_bundled_maxwell_hdf5_plugin()
    require_hdf5_filter_capabilities(401, require_decode=True)
    return plugin_path


def first_unreadable_chunk(
    input_file: Path,
    raw_dataset_path: str,
    start: int,
    stop: int,
    chunk_samples: int,
) -> tuple[int, int, BaseException]:
    for chunk_start in range(start, stop, chunk_samples):
        chunk_stop = min(stop, chunk_start + chunk_samples)
        try:
            with h5py.File(input_file, "r") as file_handle:
                _ = file_handle[raw_dataset_path][:, chunk_start:chunk_stop]
        except Exception as exc:  # pragma: no cover - exercised with damaged production data
            return chunk_start, chunk_stop, exc
    raise RuntimeError(
        "A grouped raw-data read failed, but every independently reopened chunk was readable"
    )


def verify_all_raw_chunks(input_file: Path, raw_dataset_path: str) -> None:
    with h5py.File(input_file, "r") as file_handle:
        dataset = file_handle[raw_dataset_path]
        if dataset.ndim != 2:
            raise RuntimeError(
                f"Expected a two-dimensional Maxwell raw dataset, found shape {dataset.shape}"
            )
        filters = dataset_filter_ids(dataset)

    if 401 in filters:
        configure_maxwell_decoder()

    with h5py.File(input_file, "r") as file_handle:
        dataset = file_handle[raw_dataset_path]
        channel_count, sample_count = (int(value) for value in dataset.shape)
        chunk_samples = int(dataset.chunks[1]) if dataset.chunks else 1
        target_block_bytes = 64 * 1024 * 1024
        samples_per_block = max(chunk_samples, target_block_bytes // max(1, channel_count * dataset.dtype.itemsize))
        samples_per_block = max(chunk_samples, (samples_per_block // chunk_samples) * chunk_samples)
        report_interval = max(1, sample_count // 10)
        next_report = report_interval

        for start in range(0, sample_count, samples_per_block):
            stop = min(sample_count, start + samples_per_block)
            try:
                _ = dataset[:, start:stop]
            except Exception as exc:
                chunk_start, chunk_stop, chunk_exc = first_unreadable_chunk(
                    input_file,
                    raw_dataset_path,
                    start,
                    stop,
                    chunk_samples,
                )
                raise RuntimeError(
                    "Maxwell compressed source data is unreadable at "
                    f"{raw_dataset_path} samples {chunk_start}:{chunk_stop}: {chunk_exc}"
                ) from exc
            if stop >= next_report or stop == sample_count:
                print(
                    f"[validate_maxwell_input] Full-read progress: {stop}/{sample_count} "
                    f"samples ({100 * stop / sample_count:.1f}%)",
                    flush=True,
                )
                while next_report <= stop:
                    next_report += report_interval

    print(
        "[validate_maxwell_input] Verified every raw data chunk: "
        f"{input_file} ({raw_dataset_path}, samples={sample_count}, channels={channel_count})",
        flush=True,
    )


def main() -> None:
    args = parse_args()
    input_file = Path(args.input_file)

    if not input_file.exists():
        fail(f"Input file does not exist: {input_file}")

    if not input_file.name.endswith(".raw.h5"):
        fail(
            "Unsupported file name. Expected Maxwell file ending in '.raw.h5'. "
            f"Got: {input_file.name}"
        )

    try:
        with h5py.File(input_file, "r") as file_handle:
            _ = set(file_handle.keys())
    except Exception as exc:  # pragma: no cover - defensive in runtime-only path
        fail(f"Could not read input as HDF5 Maxwell recording: {exc}")

    try:
        raw_dataset_path = discover_routed_raw_dataset_path(
            input_file,
            requested_path=args.maxwell_raw_dataset_path,
            require_single=args.maxwell_raw_dataset_path.lower() == "auto",
        )
    except Exception as exc:
        fail(str(exc))

    explicit_dataset = args.maxwell_raw_dataset_path.strip().lower() != "auto"
    if raw_dataset_path == "/data_store/data0000/groups/routed/raw" and not explicit_dataset:
        interface_class = load_maxone_interface_class()
        try:
            plugin_path = configure_maxwell_decoder()
            interface_input_file = input_file
            shared_channels = mapping_has_duplicate_channels(input_file, raw_dataset_path)
            other_recordings = well_has_other_recordings(input_file, raw_dataset_path)
            if shared_channels or other_recordings:
                # Channels wired to several electrodes, or a well that lists recordings without this
                # raw dataset; parse through the same view the converter uses.
                interface_input_file = create_maxone_compatibility_view(
                    source_file=input_file,
                    raw_dataset_path=raw_dataset_path,
                    output_file=Path("maxwell_validate_compat.raw.h5").resolve(),
                    deduplicate_mapping=shared_channels,
                    single_recording=other_recordings,
                )
            interface = interface_class(
                file_path=str(interface_input_file),
                hdf5_plugin_path=plugin_path,
                download_plugin=False,
                verbose=True,
            )
            _ = interface.get_metadata()
        except Exception as exc:  # pragma: no cover - runtime validation path
            fail(
                "NeuroConv MaxOneRecordingInterface could not parse this file as Max One data. "
                f"Error: {exc}"
            )
    elif explicit_dataset and count_routed_raw_dataset_paths(input_file) > 1:
        print(
            "[validate_maxwell_input] Explicit Maxwell data table selected from a multi-table file; "
            "conversion will use a single-table compatibility view."
        )

    if args.verify_all_raw_chunks:
        try:
            verify_all_raw_chunks(input_file, raw_dataset_path)
        except Exception as exc:
            fail(str(exc))

    with open(args.output_raw_dataset_path_file, "w", encoding="utf-8") as handle:
        handle.write(f"{raw_dataset_path}\n")

    print(
        "[validate_maxwell_input] Input accepted as supported Maxwell candidate: "
        f"{input_file} (raw_dataset_path={raw_dataset_path})"
    )


if __name__ == "__main__":
    main()
