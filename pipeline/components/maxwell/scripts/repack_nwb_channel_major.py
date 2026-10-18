#!/usr/bin/env python3
"""Repack NWB ElectricalSeries/data chunk allocation order to channel-major."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
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



def _object_key(obj):
    info = h5py.h5o.get_info(obj.id)
    return info.fileno, info.addr


def _has_references(dtype):
    dtype = np.dtype(dtype)
    if h5py.check_dtype(ref=dtype) is not None:
        return True
    if dtype.names:
        return any(_has_references(dtype.fields[name][0]) for name in dtype.names)
    if dtype.subdtype:
        return _has_references(dtype.subdtype[0])
    variable = h5py.check_dtype(vlen=dtype)
    return isinstance(variable, np.dtype) and _has_references(variable)


def _copy_nwb_graph(source, destination, target_dataset_path):
    """Allocate each physical object once, then translate references and attributes.

    H5Ocopy with expand_refs can add root aliases and independent copies of
    reference targets. Metadata pointers must instead resolve into the complete
    destination graph. Preserve existing internal hard/soft links and HDF5 types;
    external links/storage and VDSs require an explicit self-contained source.
    """
    target = source[target_dataset_path]
    if not isinstance(target, h5py.Dataset):
        raise ValueError('Repack target must be a dataset: ' + target_dataset_path)
    target_key = _object_key(target)
    copied = {_object_key(source): destination}
    pairs = [(source, destination)]
    soft_links = []

    def allocate(src, parent, name):
        key = _object_key(src)
        if key in copied:
            parent[name] = copied[key]
            return
        if isinstance(src, h5py.Group):
            dst = h5py.Group(h5py.h5g.create(parent.id, name.encode('utf-8'),
                                          gcpl=src.id.get_create_plist()))
        elif isinstance(src, h5py.Dataset):
            if src.is_virtual or src.external:
                raise ValueError('NWB repack requires a self-contained physical dataset: ' + src.name)
            if key == target_key:
                path = parent.name.rstrip('/') + '/' + name
                dst = _create_empty_dataset_like(src, destination, path)
            else:
                source.copy(src, parent, name=name, without_attrs=True, expand_refs=False)
                dst = parent[name]
        else:
            raise ValueError('Unsupported NWB HDF5 object: ' + src.name)
        copied[key] = dst
        pairs.append((src, dst))
        if isinstance(src, h5py.Group):
            allocate_children(src, dst)

    def allocate_children(src, dst):
        for name in src:
            link = src.get(name, getlink=True)
            if isinstance(link, h5py.SoftLink):
                soft_links.append((src, dst, name, link))
            elif isinstance(link, h5py.ExternalLink):
                raise ValueError('NWB repack requires a self-contained source; external link: '
                                 + src.name.rstrip('/') + '/' + name)
            else:
                allocate(src[name], dst, name)

    allocate_children(source, destination)
    if target_key not in copied:
        raise ValueError('Repack target is outside the retained hard-linked source graph.')
    for src, dst, name, link in soft_links:
        try:
            key = _object_key(src[name])
        except (KeyError, RuntimeError) as error:
            raise ValueError('Dangling NWB soft link: ' + src.name.rstrip('/') + '/' + name) from error
        if key not in copied:
            raise ValueError('NWB soft link leaves the retained source graph: ' + name)
        dst[name] = h5py.SoftLink(link.path)

    def reference(value, owner):
        if not value:
            return type(value)()
        try:
            original = owner.file[value]
        except (KeyError, RuntimeError, ValueError) as error:
            raise ValueError('Invalid NWB reference in ' + owner.name) from error
        key = _object_key(original)
        if key not in copied:
            raise ValueError('NWB reference leaves the retained source graph in ' + owner.name)
        dst = copied[key]
        if isinstance(value, h5py.RegionReference):
            region = h5py.h5r.get_region(value, owner.file.id)
            return h5py.h5r.create(destination.id, dst.name.encode('utf-8'), h5py.h5r.DATASET_REGION, region)
        return dst.ref

    def translate(value, dtype, owner):
        dtype = np.dtype(dtype)
        if not _has_references(dtype):
            return value
        if dtype.subdtype:
            return translate(value, dtype.subdtype[0], owner)
        result = np.array(value, dtype=dtype, copy=True)
        if h5py.check_dtype(ref=dtype) is not None:
            for index in np.ndindex(result.shape):
                result[index] = reference(result[index], owner)
        elif dtype.names:
            for name in dtype.names:
                field = dtype.fields[name][0]
                if _has_references(field):
                    result[name] = translate(result[name], field, owner)
        else:
            base = h5py.check_dtype(vlen=dtype)
            for index in np.ndindex(result.shape):
                result[index] = translate(result[index], base, owner)
        return result[()] if result.shape == () else result

    for src, dst in pairs:
        if isinstance(src, h5py.Dataset) and _has_references(src.dtype) and src.shape is not None:
            if isinstance(h5py.check_dtype(vlen=src.dtype), np.dtype):
                # Assign one vlen element at a time: bulk assignment in the
                # pinned h5py treats the outer arrays as individual references.
                for index in np.ndindex(src.shape):
                    dst[index] = translate(src[index], h5py.check_dtype(vlen=src.dtype), src)
            elif src.ndim == 0:
                dst[()] = translate(src[()], src.dtype, src)
            else:
                row_bytes = max(1, int(np.prod(src.shape[1:])) * src.dtype.itemsize)
                rows = max(1, min(1024, (8 * 1024 * 1024) // row_bytes))
                for start in range(0, src.shape[0], rows):
                    selection = slice(start, min(start + rows, src.shape[0]))
                    dst[selection] = translate(src[selection], src.dtype, src)
        for name, value in src.attrs.items():
            original = src.attrs.get_id(name)
            dtype = original.dtype
            # Keep the exact HDF5 datatype/space, including string padding in
            # dimension-scale CLASS and nested reference attribute types.
            attribute = h5py.h5a.create(dst.id, name.encode('utf-8'), original.get_type(), original.get_space())
            try:
                if not isinstance(value, h5py.Empty):
                    data = np.asarray(translate(value, dtype, src), dtype=dtype)
                    attribute.write(data, mtype=h5py.h5t.py_create(dtype))
            finally:
                attribute.close()
    return copied[target_key]


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
    if input_file.resolve() == output_file.resolve():
        fail("Input and output must be different files.")
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(str(input_file), "r") as src_file:
        if dataset_path not in src_file:
            fail(f"Target dataset path not found: {dataset_path}")
        src_dataset = src_file[dataset_path]
        # Publish the local result only after the full graph and chunks succeed.
        # Existing output and input remain intact on an invalid reference.
        with tempfile.TemporaryDirectory(prefix='.' + output_file.name + '-', dir=output_file.parent) as scratch:
            temporary = Path(scratch) / output_file.name
            with h5py.File(temporary, "w") as dst_file:
                dst_dataset = _copy_nwb_graph(src_file, dst_file, dataset_path)
                _copy_chunks_channel_major(src_dataset=src_dataset, dst_dataset=dst_dataset)
                dst_file.flush()
            os.replace(temporary, output_file)
    print(
        "[repack_nwb_channel_major] Wrote repacked NWB file: "
        f"{output_file} (dataset={dataset_path})"
    )


if __name__ == "__main__":
    main()
