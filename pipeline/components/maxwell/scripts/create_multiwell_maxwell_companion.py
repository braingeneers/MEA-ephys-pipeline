#!/usr/bin/env python3
"""Create one compact Maxwell file whose routed raw tables reference sibling NWBs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import h5py

from create_legacy_maxwell_companion import (
    DEFAULT_NWB_DATASET_PATH,
    _build_virtual_layout,
    fail,
)
from maxwell_h5_utils import list_routed_raw_dataset_paths
from maxwell_companion_graph import companion_output, copy_companion_graph


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--mapping-json", required=True)
    parser.add_argument("--output-file", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_file = Path(args.input_file)
    output_file = Path(args.output_file)
    mappings = json.loads(Path(args.mapping_json).read_text(encoding="utf-8"))
    if not mappings:
        fail("At least one Maxwell-to-NWB mapping is required.")
    targets = {item["raw_dataset_path"] for item in mappings}
    if len(targets) != len(mappings):
        fail("Each companion mapping must target a unique Maxwell raw dataset.")

    with h5py.File(input_file, "r") as source:
        source_targets = set(list_routed_raw_dataset_paths(source))
    if targets != source_targets:
        missing = sorted(source_targets - targets)
        unknown = sorted(targets - source_targets)
        details = []
        if missing:
            details.append(f"missing mappings: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown mappings: {', '.join(unknown)}")
        fail("Companion mappings must cover every routed raw table; " + "; ".join(details))

    if output_file.resolve() == input_file.resolve() or any(
            output_file.resolve() == Path(item['local_nwb_file']).resolve() for item in mappings):
        fail("Companion output must differ from the Maxwell and NWB inputs.")
    with h5py.File(input_file, "r") as source:
        layouts = {}
        for item in mappings:
            target = item["raw_dataset_path"]
            if (Path(item['nwb_sibling_filename']).name != item['nwb_sibling_filename']
                    or not item['nwb_sibling_filename'].endswith('.nwb')):
                fail("Every NWB sibling must be a filename ending in .nwb.")
            raw = source[target]
            with h5py.File(item["local_nwb_file"], "r") as nwb:
                nwb_path = item.get("nwb_dataset_path", DEFAULT_NWB_DATASET_PATH)
                if nwb_path not in nwb:
                    fail(f"NWB dataset path not found: {nwb_path}")
                layouts[target] = _build_virtual_layout(
                    raw_shape=tuple(int(value) for value in raw.shape),
                    raw_dtype=raw.dtype,
                    nwb_shape=tuple(int(value) for value in nwb[nwb_path].shape),
                    nwb_dtype=nwb[nwb_path].dtype,
                    nwb_sibling_filename=item["nwb_sibling_filename"],
                    nwb_dataset_path=nwb_path,
                )
        with companion_output(output_file) as destination:
            copy_companion_graph(source, destination, layouts)
            for item in mappings:
                linked = destination[item['raw_dataset_path']]
                linked.attrs["legacy_data_bridge"] = "virtual_dataset_to_nwb"
                linked.attrs["legacy_data_bridge_target_file"] = item["nwb_sibling_filename"]
                linked.attrs["legacy_data_bridge_target_dataset"] = item.get(
                    "nwb_dataset_path", DEFAULT_NWB_DATASET_PATH
                )
                linked.attrs["legacy_data_bridge_source_raw_dataset"] = item['raw_dataset_path']

            destination.attrs["braingeneers_linked_companion"] = True
            destination.attrs["braingeneers_linked_companion_scope"] = "whole_recording"
            destination.attrs["braingeneers_linked_companion_mapping_count"] = len(mappings)

    print(f"[create_multiwell_maxwell_companion] Wrote {output_file} with {len(mappings)} linked raw tables")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[create_multiwell_maxwell_companion] {exc}", file=sys.stderr)
        raise
