#!/usr/bin/env python3
"""Create a compact Maxwell companion file that virtualizes the raw table from NWB."""

from __future__ import annotations

from workflow_defaults import scientific_default

import argparse
import sys
from pathlib import Path

import h5py
import numpy as np

from maxwell_h5_utils import discover_routed_raw_dataset_path, list_routed_raw_dataset_paths
from maxwell_companion_graph import companion_output, copy_companion_graph, object_key


DEFAULT_NWB_DATASET_PATH = "/acquisition/ElectricalSeries/data"


def fail(message: str) -> None:
    print(f"[create_legacy_maxwell_companion] {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", required=True, help="Path to source Maxwell .raw.h5 file")
    parser.add_argument("--nwb-file", required=True, help="Path to repacked NWB file")
    parser.add_argument("--output-file", required=True, help="Path to compact Maxwell companion .raw.h5")
    parser.add_argument(
        "--nwb-sibling-filename",
        required=True,
        help="Published NWB sibling filename that the companion file should reference",
    )
    parser.add_argument(
        "--maxwell-raw-dataset-path",
        default=scientific_default('maxwell_raw_dataset_path'),
        help="Dataset path for the large Maxwell raw table to replace, or 'auto' to discover it",
    )
    parser.add_argument(
        "--nwb-dataset-path",
        default=DEFAULT_NWB_DATASET_PATH,
        help="Dataset path in the NWB file that stores the authoritative signal data",
    )
    return parser.parse_args()


def _build_virtual_layout(
    *,
    raw_shape: tuple[int, int],
    raw_dtype,
    nwb_shape: tuple[int, int],
    nwb_dtype,
    nwb_sibling_filename: str,
    nwb_dataset_path: str,
) -> h5py.VirtualLayout:
    if len(raw_shape) != 2:
        fail(f"Expected Maxwell raw dataset to be 2D, found shape {raw_shape}")
    if len(nwb_shape) != 2:
        fail(f"Expected NWB ElectricalSeries/data to be 2D, found shape {nwb_shape}")

    n_channels, n_timepoints = (int(x) for x in raw_shape)
    nwb_timepoints, nwb_channels = (int(x) for x in nwb_shape)

    if (n_channels, n_timepoints) != (nwb_channels, nwb_timepoints):
        fail(
            "Maxwell raw dataset shape does not match NWB data after transpose: "
            f"raw_shape={raw_shape}, nwb_shape={nwb_shape}"
        )
    if raw_dtype != nwb_dtype:
        fail(
            "Maxwell raw dataset dtype does not match NWB data dtype: "
            f"raw_dtype={raw_dtype}, nwb_dtype={nwb_dtype}"
        )

    source = h5py.VirtualSource(nwb_sibling_filename, nwb_dataset_path, shape=nwb_shape)
    layout = h5py.VirtualLayout(shape=raw_shape, dtype=raw_dtype)
    for channel_index in range(n_channels):
        layout[channel_index, :] = source[:, channel_index]
    return layout


def main() -> None:
    args = parse_args()

    input_file = Path(args.input_file)
    nwb_file = Path(args.nwb_file)
    output_file = Path(args.output_file)

    if not input_file.exists():
        fail(f"Input Maxwell file does not exist: {input_file}")
    if not nwb_file.exists():
        fail(f"Input NWB file does not exist: {nwb_file}")
    if not input_file.name.endswith(".raw.h5"):
        fail("Input Maxwell file must end with '.raw.h5'.")
    if not args.nwb_sibling_filename.endswith(".nwb"):
        fail("--nwb-sibling-filename must end with '.nwb'.")

    if output_file.resolve() in {input_file.resolve(), nwb_file.resolve()}:
        fail("Companion output must differ from the Maxwell and NWB inputs.")
    if Path(args.nwb_sibling_filename).name != args.nwb_sibling_filename:
        fail("--nwb-sibling-filename must be a filename in the companion's directory.")

    try:
        raw_dataset_path = discover_routed_raw_dataset_path(
            input_file,
            requested_path=args.maxwell_raw_dataset_path,
            require_single=args.maxwell_raw_dataset_path.lower() == "auto",
        )
    except Exception as exc:
        fail(str(exc))

    # Persist the original store/well paths, not NeuroConv's temporary view.
    # A partial multi-table selection omits other data groups at every alias.
    companion_raw_path = raw_dataset_path
    with h5py.File(str(input_file), "r") as src_file:
        if companion_raw_path not in src_file:
            fail(f"Maxwell raw dataset path not found: {companion_raw_path}")
        raw_dataset = src_file[companion_raw_path]
        raw_paths = list_routed_raw_dataset_paths(src_file)
        if raw_dataset_path not in raw_paths:
            fail("The companion requires an actual routed data_store raw dataset.")
        excluded = set()
        if len(raw_paths) > 1:
            data_path = raw_dataset_path[:-len('/groups/routed/raw')]
            selected = object_key(src_file[data_path])
            excluded.update(object_key(group) for group in src_file['data_store'].values()
                            if object_key(group) != selected)
            excluded.update(object_key(src_file[path]) for path in raw_paths if path != raw_dataset_path)
            for routing in ('wells', 'recordings'):
                if routing in src_file:
                    excluded.update(object_key(group) for group in src_file[routing].values()
                                    if not any(object_key(child) == selected for child in group.values()))

        with h5py.File(str(nwb_file), "r") as nwb_handle:
            if args.nwb_dataset_path not in nwb_handle:
                fail(f"NWB dataset path not found: {args.nwb_dataset_path}")
            nwb_dataset = nwb_handle[args.nwb_dataset_path]

            layout = _build_virtual_layout(
                raw_shape=tuple(int(x) for x in raw_dataset.shape),
                raw_dtype=raw_dataset.dtype,
                nwb_shape=tuple(int(x) for x in nwb_dataset.shape),
                nwb_dtype=nwb_dataset.dtype,
                nwb_sibling_filename=args.nwb_sibling_filename,
                nwb_dataset_path=args.nwb_dataset_path,
            )

        with companion_output(output_file) as dst_file:
            copy_companion_graph(src_file, dst_file, {companion_raw_path: layout}, excluded=excluded)
            companion_raw_dataset = dst_file[companion_raw_path]
            companion_raw_dataset.attrs["legacy_data_bridge"] = "virtual_dataset_to_nwb"
            companion_raw_dataset.attrs["legacy_data_bridge_target_file"] = args.nwb_sibling_filename
            companion_raw_dataset.attrs["legacy_data_bridge_target_dataset"] = args.nwb_dataset_path
            companion_raw_dataset.attrs["legacy_data_bridge_source_raw_dataset"] = raw_dataset_path
            dst_file.attrs["braingeneers_linked_companion_scope"] = "selected_unit"
            dst_file.attrs["braingeneers_linked_companion_mapping_count"] = 1

    print(
        "[create_legacy_maxwell_companion] Wrote compact Maxwell companion: "
        f"{output_file} (raw_dataset={companion_raw_path}, "
        f"nwb_sibling={args.nwb_sibling_filename}, nwb_dataset={args.nwb_dataset_path})"
    )


if __name__ == "__main__":
    main()
