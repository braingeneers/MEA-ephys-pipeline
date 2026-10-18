#!/usr/bin/env python3
"""Repack NWB ElectricalSeries/data chunk allocation order to channel-major."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import h5py
import numpy as np


def fail(message: str) -> None:
    print(f"[repack_nwb_channel_major] {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", required=True, help="Input NWB file path")
    parser.add_argument("--output-file", required=True, help="Output NWB file path")
    parser.add_argument(
        "--dataset-path",
        default="/acquisition/ElectricalSeries/data",
        help="Dataset path to repack",
    )
    return parser.parse_args()


def _remap_reference_value(value, src_file: h5py.File, dst_file: h5py.File):
    if isinstance(value, h5py.Reference):
        if not value:
            return value
        src_obj = src_file[value]
        if src_obj.name in dst_file:
            return dst_file[src_obj.name].ref
        return value

    if hasattr(value, "dtype"):
        ref_dtype = h5py.check_dtype(ref=value.dtype)
        if ref_dtype is not None:
            remapped = value.copy()
            it = np.nditer(remapped, flags=["multi_index", "refs_ok"], op_flags=["readwrite"])
            for ref_cell in it:
                ref_cell[...] = _remap_reference_value(ref_cell[()], src_file, dst_file)
            return remapped

    return value


def _copy_group_attrs_with_ref_remap(
    *,
    src_group: h5py.Group,
    dst_group: h5py.Group,
    src_file: h5py.File,
    dst_file: h5py.File,
) -> None:
    for key, value in src_group.attrs.items():
        dst_group.attrs[key] = _remap_reference_value(value, src_file, dst_file)


def _copy_dataset_attrs_with_ref_remap(
    *,
    src_dataset: h5py.Dataset,
    dst_dataset: h5py.Dataset,
    src_file: h5py.File,
    dst_file: h5py.File,
    skip_dimension_attrs: bool = False,
) -> None:
    for key, value in src_dataset.attrs.items():
        if skip_dimension_attrs and key in {"DIMENSION_LIST", "REFERENCE_LIST"}:
            continue
        dst_dataset.attrs[key] = _remap_reference_value(value, src_file, dst_file)


def _copy_tree_except_target(
    *,
    src_group: h5py.Group,
    dst_group: h5py.Group,
    target_dataset_path: str,
    groups_needing_attr_copy: list[str],
    datasets_needing_attr_copy: list[str],
) -> None:
    groups_needing_attr_copy.append(src_group.name)

    for name in src_group.keys():
        obj = src_group[name]
        src_path = obj.name

        if isinstance(obj, h5py.Dataset):
            if src_path == target_dataset_path:
                continue
            src_group.copy(name, dst_group, name=name, expand_refs=True)
            datasets_needing_attr_copy.append(src_path)
            continue

        if isinstance(obj, h5py.Group):
            if target_dataset_path.startswith(f"{src_path}/"):
                nested = dst_group.require_group(name)
                _copy_tree_except_target(
                    src_group=obj,
                    dst_group=nested,
                    target_dataset_path=target_dataset_path,
                    groups_needing_attr_copy=groups_needing_attr_copy,
                    datasets_needing_attr_copy=datasets_needing_attr_copy,
                )
            else:
                src_group.copy(name, dst_group, name=name, expand_refs=True)
            continue

        fail(f"Unhandled HDF5 object type at path {src_path}: {type(obj)}")


def _create_empty_dataset_like(src_dataset: h5py.Dataset, dst_file: h5py.File, dataset_path: str) -> h5py.Dataset:
    link_create_plist = h5py.h5p.create(h5py.h5p.LINK_CREATE)
    link_create_plist.set_create_intermediate_group(1)
    dataset_id = h5py.h5d.create(
        dst_file.id,
        dataset_path.encode(),
        src_dataset.id.get_type(),
        src_dataset.id.get_space(),
        lcpl=link_create_plist,
        dcpl=src_dataset.id.get_create_plist(),
    )
    return h5py.Dataset(dataset_id)


def _copy_dataset_attrs_and_scales(
    *,
    src_dataset: h5py.Dataset,
    dst_dataset: h5py.Dataset,
    dst_file: h5py.File,
) -> None:
    for key, value in src_dataset.attrs.items():
        if key in {"DIMENSION_LIST", "REFERENCE_LIST"}:
            continue
        dst_dataset.attrs[key] = value

    for axis in range(src_dataset.ndim):
        label = src_dataset.dims[axis].label
        if label:
            dst_dataset.dims[axis].label = label

        for scale in src_dataset.dims[axis]:
            if scale.name in dst_file:
                dst_dataset.dims[axis].attach_scale(dst_file[scale.name])


def _copy_chunks_channel_major(*, src_dataset: h5py.Dataset, dst_dataset: h5py.Dataset) -> None:
    if src_dataset.chunks is None:
        fail("Target dataset is not chunked; cannot repack chunk allocation order.")

    if len(src_dataset.shape) != 2:
        fail(f"Expected 2D dataset at target path, found shape {src_dataset.shape}.")

    n_time, n_channels = (int(x) for x in src_dataset.shape)
    chunk_time, chunk_channels = (int(x) for x in src_dataset.chunks)
    total_chunks = ((n_time + chunk_time - 1) // chunk_time) * ((n_channels + chunk_channels - 1) // chunk_channels)
    copied_chunks = 0

    for channel_start in range(0, n_channels, chunk_channels):
        for time_start in range(0, n_time, chunk_time):
            offset = (time_start, channel_start)
            filter_mask, chunk_bytes = src_dataset.id.read_direct_chunk(offset)
            dst_dataset.id.write_direct_chunk(offset, chunk_bytes, filter_mask=filter_mask)

            copied_chunks += 1
            if copied_chunks % 100000 == 0:
                print(
                    "[repack_nwb_channel_major] Copied "
                    f"{copied_chunks}/{total_chunks} chunks...",
                    flush=True,
                )


def main() -> None:
    args = parse_args()

    input_file = Path(args.input_file)
    output_file = Path(args.output_file)
    dataset_path = args.dataset_path

    if not input_file.exists():
        fail(f"Input file does not exist: {input_file}")

    if output_file.exists():
        output_file.unlink()

    output_file.parent.mkdir(parents=True, exist_ok=True)

    with h5py.File(str(input_file), "r") as src_file:
        if dataset_path not in src_file:
            fail(f"Target dataset path not found: {dataset_path}")
        src_dataset = src_file[dataset_path]

        with h5py.File(str(output_file), "w") as dst_file:
            groups_needing_attr_copy: list[str] = []
            datasets_needing_attr_copy: list[str] = []
            _copy_tree_except_target(
                src_group=src_file,
                dst_group=dst_file,
                target_dataset_path=dataset_path,
                groups_needing_attr_copy=groups_needing_attr_copy,
                datasets_needing_attr_copy=datasets_needing_attr_copy,
            )

            dst_dataset = _create_empty_dataset_like(
                src_dataset=src_dataset,
                dst_file=dst_file,
                dataset_path=dataset_path,
            )
            _copy_dataset_attrs_and_scales(
                src_dataset=src_dataset,
                dst_dataset=dst_dataset,
                dst_file=dst_file,
            )
            _copy_dataset_attrs_with_ref_remap(
                src_dataset=src_dataset,
                dst_dataset=dst_dataset,
                src_file=src_file,
                dst_file=dst_file,
                skip_dimension_attrs=True,
            )
            for group_path in groups_needing_attr_copy:
                _copy_group_attrs_with_ref_remap(
                    src_group=src_file[group_path],
                    dst_group=dst_file[group_path],
                    src_file=src_file,
                    dst_file=dst_file,
                )
            for dataset_path_to_fix in datasets_needing_attr_copy:
                _copy_dataset_attrs_with_ref_remap(
                    src_dataset=src_file[dataset_path_to_fix],
                    dst_dataset=dst_file[dataset_path_to_fix],
                    src_file=src_file,
                    dst_file=dst_file,
                )
            _copy_chunks_channel_major(src_dataset=src_dataset, dst_dataset=dst_dataset)
            dst_file.flush()

    print(
        "[repack_nwb_channel_major] Wrote repacked NWB file: "
        f"{output_file} (dataset={dataset_path})"
    )


if __name__ == "__main__":
    main()
