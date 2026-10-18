"""Copy a Maxwell object graph once, replacing raw objects with sibling-NWB VDSs.

References belong to their physical HDF5 file, not the path used to reach an
external/hard/soft link. Allocate the complete retained graph before translating
references. Never expand references into additional copies of raw datasets.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile

import h5py
import numpy as np
from maxwell_h5_utils import list_routed_raw_dataset_paths


def object_key(obj):
    info = h5py.h5o.get_info(obj.id)
    return (info.fileno, info.addr)


def has_references(dtype):
    dtype = np.dtype(dtype)
    if h5py.check_dtype(ref=dtype) is not None:
        return True
    if dtype.names:
        return any(has_references(dtype.fields[name][0]) for name in dtype.names)
    if dtype.subdtype:
        return has_references(dtype.subdtype[0])
    variable = h5py.check_dtype(vlen=dtype)
    return isinstance(variable, np.dtype) and has_references(variable)


@contextmanager
def companion_output(path):
    """Keep an existing result intact if graph copying or verification fails."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    os.close(descriptor)
    try:
        with h5py.File(temporary, 'w', libver='latest') as destination:
            yield destination
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def copy_companion_graph(source, destination, replacements, *, excluded=()):
    """Materialize retained links locally, preserving object identity and references.

    `replacements` maps original logical dataset paths to validated VirtualLayouts.
    `excluded` contains physical object keys omitted by a selected-unit projection.
    A reference outside the retained graph is an error, except dimension-scale
    backreferences to omitted users, which are projected to retained datasets.
    Non-target VDS/external-storage datasets are rejected rather than retaining a
    hidden dependency. Ordinary dataset bytes/creation properties use native H5Ocopy.
    """
    excluded = set(excluded)
    layouts = {}
    for path, layout in replacements.items():
        key = object_key(source[path])
        if key in layouts or key in excluded:
            raise ValueError('Companion mappings must identify distinct retained raw objects: ' + path)
        layouts[key] = layout
    copied = {object_key(source): destination}
    pairs = [(source, destination)]
    physical_raw = {}

    def allocate(src, parent, name):
        key = object_key(src)
        if key in excluded:
            return
        if key in copied:
            parent[name] = copied[key]
            return
        if isinstance(src, h5py.Group):
            dst = parent.create_group(name, track_order=bool(
                src.id.get_create_plist().get_link_creation_order() & h5py.h5p.CRT_ORDER_TRACKED))
        elif isinstance(src, h5py.Dataset) and key in layouts:
            dst = parent.create_virtual_dataset(name, layouts[key], fillvalue=src.fillvalue)
        elif isinstance(src, h5py.Dataset):
            file_number = key[0]
            if file_number not in physical_raw:
                physical_raw[file_number] = {object_key(src.file[path])
                    for path in list_routed_raw_dataset_paths(src.file)}
            if key in physical_raw[file_number]:
                raise ValueError('Unmapped routed raw object in companion graph: ' + src.name)
            if src.is_virtual or src.external:
                raise ValueError('Unmapped dataset has external storage: ' + src.name)
            src.file.copy(src, parent, name=name, without_attrs=True, expand_refs=False)
            dst = parent[name]
        elif isinstance(src, h5py.Datatype):
            src.file.copy(src, parent, name=name, without_attrs=True, expand_refs=False)
            dst = parent[name]
        else:
            raise ValueError('Unsupported companion object: ' + src.name)
        copied[key] = dst
        pairs.append((src, dst))
        if isinstance(src, h5py.Group):
            for child in src:
                # Dereferencing also materializes external and soft links. A
                # dangling link fails here instead of becoming a hidden dependency.
                allocate(src[child], dst, child)

    for name in source:
        allocate(source[name], destination, name)
    if not set(layouts).issubset(copied):
        raise ValueError('A mapped raw dataset is outside the retained companion graph.')

    def reference(value, owner):
        if not value:
            return type(value)()
        target = owner.file[value]
        key = object_key(target)
        if key not in copied:
            raise ValueError('Reference leaves the selected companion graph: ' + target.name)
        dst = copied[key]
        if isinstance(value, h5py.RegionReference):
            region = h5py.h5r.get_region(value, owner.file.id)
            return h5py.h5r.create(destination.id, dst.name.encode(), h5py.h5r.DATASET_REGION, region)
        return dst.ref

    def translate(value, dtype, owner):
        dtype = np.dtype(dtype)
        if not has_references(dtype):
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
                if has_references(field):
                    result[name] = translate(result[name], field, owner)
        else:
            base = h5py.check_dtype(vlen=dtype)
            for index in np.ndindex(result.shape):
                result[index] = translate(result[index], base, owner)
        return result[()] if result.shape == () else result

    for src, dst in pairs:
        if isinstance(src, h5py.Dataset) and has_references(src.dtype):
            # Reference payloads can be large too. Bound fixed-row blocks rather
            # than loading the entire dataset merely to translate its pointers.
            if src.shape is None:
                pass
            elif isinstance(h5py.check_dtype(vlen=src.dtype), np.dtype):
                # h5py 3.16 bulk assignment of vlen reference arrays attempts
                # to convert the outer arrays as individual references.
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
            space = original.get_space()
            if (name == 'REFERENCE_LIST' and isinstance(src, h5py.Dataset) and src.is_scale
                    and dtype.names == ('dataset', 'dimension')):
                value = value[[not ref or object_key(src.file[ref]) in copied
                               for ref in value['dataset']]]
                if not len(value):
                    continue
                space = h5py.h5s.create_simple(value.shape)
            # NumPy's dtype loses HDF5 string padding. In particular, the
            # dimension-scale CLASS marker requires the original NULLTERM type.
            attribute = h5py.h5a.create(dst.id, name.encode('utf-8'), original.get_type(), space)
            try:
                if not isinstance(value, h5py.Empty):
                    translated = np.asarray(translate(value, dtype, src), dtype=dtype)
                    attribute.write(translated, mtype=h5py.h5t.py_create(dtype))
            finally:
                attribute.close()
