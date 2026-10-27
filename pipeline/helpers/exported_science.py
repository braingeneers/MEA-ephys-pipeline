"""Complete decoded scientific identities for self-contained derived NWB stores.

Numeric data are streamed in C order. Object data retain typed strings, compound
fields and complete ordered reference targets, rather than pickle or file hashes.
"""
import hashlib
import json
from pathlib import Path
import struct
from collections import Counter

import portable_bundle as portable

BLOCK_BYTES = 8 * 1024 * 1024
BLOCK_ROWS = 65536


def require(condition, message):
    if not condition:
        raise portable.BundleError(message)


def bind_units(data, sources):
    """Bind every exported unit/train/QC flag to the complete source sorting.

    The collector calls its saved sorting 'curated', but retains all analyzer
    units with a boolean default_qc property. QC selection belongs to reports.
    Compare the complete row inventory across stores; the decoded receipt below
    additionally freezes each store's row order, properties and reference graph.
    """
    import numpy as np
    import spikeinterface as si

    def unit_key(value):
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        if isinstance(value, np.generic):
            value = value.item()
        require(type(value) in (int, str), "Invalid derived NWB source unit identity")
        return [type(value).__name__, value]

    def row_key(unit, qc, times):
        return encoded([unit_key(unit), bool(qc), array_identity(times)])

    expected = Counter()
    source_lengths = {}
    for name, analyzer, recording, qc in sources:
        require(isinstance(qc, np.ndarray) and qc.dtype == np.dtype(bool)
                and qc.shape == analyzer.unit_ids.shape,
                f"Invalid source QC mask for derived NWB: {name}")
        sorting = si.load(data / "collected/curated" / name)
        require(sorting.get_num_segments() == analyzer.sorting.get_num_segments() == 1,
                f"Derived NWB requires one sorting segment for {name}")
        require(array_identity(sorting.unit_ids) == array_identity(analyzer.unit_ids)
                and sorting.sampling_frequency == analyzer.sorting.sampling_frequency,
                f"Collected unit identity differs from analyzer for {name}")
        saved_qc = sorting.get_property("default_qc")
        require(isinstance(saved_qc, np.ndarray) and saved_qc.dtype == qc.dtype
                and saved_qc.shape == qc.shape and saved_qc.tobytes() == qc.tobytes(),
                f"Collected QC flags differ from curation for {name}")
        for unit in sorting.unit_ids:
            actual = sorting.get_unit_spike_train(unit)
            original = analyzer.sorting.get_unit_spike_train(unit)
            require(actual.dtype == original.dtype and actual.shape == original.shape
                    and actual.tobytes() == original.tobytes(),
                    f"Collected spike train differs from analyzer for {name}: {unit!r}")
        # Match the writer's register_recording call, including explicit clocks.
        sorting.register_recording(recording)
        for unit, flag in zip(sorting.unit_ids, qc):
            times = sorting.get_unit_spike_train(unit, return_times=True)
            key = row_key(unit, flag, times)
            expected[key] += 1
            source_lengths[key] = len(times)

    observed = Counter()
    paths = sorted((data / "derived-nwb").glob("*.nwb*"))
    require(paths, "Final export produced no derived NWB")
    for path in paths:
        if path.is_dir():
            import zarr
            group = zarr.open_group(str(path), mode="r")
        else:
            import h5py
            group = h5py.File(path, "r")
        try:
            require("nwb_version" in group.attrs, f"Derived export is not NWB: {path.name}")
            if "units" not in group:
                continue
            table = group["units"]
            require(all(key in table for key in ("id", "ks_unit_id", "default_qc", "spike_times", "spike_times_index")),
                    f"Incomplete derived NWB unit columns: {path.name}")
            count = len(table["id"])
            require(all(table[key].shape == (count,) for key in ("id", "ks_unit_id", "default_qc", "spike_times_index"))
                    and table["id"].dtype == np.dtype("int64")
                    and np.array_equal(table["id"][:], np.arange(count, dtype="int64"))
                    and table["default_qc"].dtype == np.dtype(bool)
                    and table["spike_times"].ndim == 1
                    and table["spike_times"].dtype == np.dtype("float64"),
                    f"Invalid derived NWB unit column dtype/shape/IDs: {path.name}")
            indices = table["spike_times_index"][:]
            lengths = []
            start = 0
            for unit, flag, end in zip(table["ks_unit_id"][:], table["default_qc"][:], indices):
                require(isinstance(end, np.integer) and start <= int(end) <= len(table["spike_times"]),
                        f"Invalid derived NWB spike index: {path.name}")
                end = int(end)
                key = row_key(unit, flag, table["spike_times"][start:end])
                observed[key] += 1
                require(observed[key] <= expected[key],
                        f"Derived NWB unit/train/QC differs from complete sorting: {path.name}")
                # Expected cumulative indices/dtype follow matched source trains,
                # never the produced buffer's own declared size.
                lengths.append(source_lengths[key])
                start = end
            require(start == len(table["spike_times"]), f"Unclaimed derived NWB spike buffer tail: {path.name}")
            bits = 8
            while start > 2 ** bits - 1:
                bits *= 2
            require(bits <= 64 and indices.dtype == np.dtype(f"uint{bits}")
                    and np.array_equal(indices, np.cumsum(lengths, dtype="uint64")),
                    f"Derived NWB spike index dtype/values differ: {path.name}")
        finally:
            if not path.is_dir():
                group.close()
    require(observed == expected, "Derived NWB unit/train/QC inventory differs from complete sorting")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def dtype_identity(dtype):
    return portable.jsonable(dtype.descr) if dtype.fields else dtype.str


def blocks(array):
    """Bound ordinary row reads and subdivide unusually wide rows as well."""
    import numpy as np

    if not array.shape:
        yield np.asarray(array[()], dtype=array.dtype)
        return
    row_bytes = max(1, array.dtype.itemsize) * int(np.prod(array.shape[1:], dtype=object))
    if row_bytes <= BLOCK_BYTES:
        rows = min(BLOCK_ROWS, max(1, BLOCK_BYTES // max(1, row_bytes)))
        if array.dtype.hasobject:
            rows = min(rows, 1024)
        for start in range(0, array.shape[0], rows):
            yield np.asarray(array[start:min(array.shape[0], start + rows)])
    else:
        width = max(1, BLOCK_BYTES // max(1, array.dtype.itemsize))
        for prefix in np.ndindex(array.shape[:-1]):
            for start in range(0, array.shape[-1], width):
                yield np.asarray(array[prefix + (slice(start, start + width),)])


def typed(value, reference=None):
    import numpy as np

    if reference is not None:
        target = reference(value)
        if target is not None:
            return {"reference": target}
    if isinstance(value, np.ndarray):
        return {"array": array_identity(value, reference)}
    if isinstance(value, np.void) and value.dtype.fields:
        return {"compound": [[key, typed(value[key], reference)] for key in value.dtype.names]}
    if isinstance(value, np.generic):
        if value.dtype.kind in "biufcmM":
            return {"numpy_scalar": value.dtype.str, "bytes": value.tobytes().hex()}
        return {"numpy_scalar": value.dtype.str, "value": typed(value.item(), reference)}
    if isinstance(value, dict):
        require(all(type(key) is str for key in value), "Derived NWB mapping keys must be strings")
        return {"mapping": [[key, typed(value[key], reference)] for key in sorted(value)]}
    if isinstance(value, (list, tuple)):
        return {type(value).__name__: [typed(item, reference) for item in value]}
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if type(value) is float:
        return {"float64_bits": struct.pack(">d", value).hex()}
    if type(value) is complex:
        return {"complex128_bits": struct.pack(">dd", value.real, value.imag).hex()}
    if value is None or type(value) in (str, bool, int):
        return {type(value).__name__: value}
    raise portable.BundleError(f"Unsupported derived NWB scientific value: {type(value).__name__}")


def array_identity(array, reference=None):
    """Cover the entire stored buffer, including ragged tails and NaN masks."""
    import numpy as np

    digest = hashlib.sha256()
    for values in blocks(array):
        require(values.dtype == array.dtype, "Derived NWB dtype changed during a read")
        if values.dtype.hasobject or values.dtype.fields:
            for item in values.reshape(-1):
                payload = encoded(typed(item, reference))
                digest.update(len(payload).to_bytes(8, "big"))
                digest.update(payload)
        else:
            digest.update(values.tobytes(order="C"))
    return {"dtype": dtype_identity(np.dtype(array.dtype)), "shape": list(array.shape),
            "algorithm": "mea.nwb-decoded-data/v1", "sha256": digest.hexdigest()}


def zarr_snapshot(path):
    import zarr

    group = zarr.open_group(str(path), mode="r")
    require("nwb_version" in group.attrs, f"Derived export is not NWB Zarr: {path.name}")
    root_id = group.attrs.get("object_id")
    nodes = {}

    def reference(value):
        if not isinstance(value, dict) or not {"source", "path", "object_id", "source_object_id"}.issubset(value):
            return None
        require(value["source"] == "." and value["source_object_id"] == root_id,
                f"Derived NWB reference source differs from its store: {path.name}")
        target_path = value["path"]
        require(isinstance(target_path, str) and target_path.startswith("/")
                and (target_path == "/" or target_path.lstrip("/") in group),
                f"Derived NWB reference target is absent: {path.name}:{target_path}")
        target = group if target_path == "/" else group[target_path.lstrip("/")]
        require(value["object_id"] is None or value["object_id"] == target.attrs.get("object_id"),
                f"Derived NWB reference target identity differs: {path.name}:{target_path}")
        return {"class": type(value).__module__ + "." + type(value).__qualname__,
                "fields": {key: typed(item) for key, item in sorted(value.items())}}

    def visit(name, obj):
        attrs = dict(obj.attrs)
        for link in attrs.get("zarr_link", []):
            require(isinstance(link, dict) and link.get("source") == "."
                    and isinstance(link.get("path"), str) and link["path"].startswith("/")
                    and (link["path"] == "/" or link["path"].lstrip("/") in group),
                    f"Derived NWB link target is absent or external: {path.name}:/{name}")
        node = {"kind": "array" if isinstance(obj, zarr.Array) else "group",
                "attrs": {key: typed(value, reference) for key, value in sorted(attrs.items())}}
        if isinstance(obj, zarr.Array):
            metadata = json.loads((path / name / ".zarray").read_text())
            node.update(data=array_identity(obj, reference), encoding=typed(metadata))
        nodes["/" + name] = node

    visit("", group)
    group.visititems(visit)
    consolidated = path / ".zmetadata"
    if consolidated.exists():
        value = json.loads(consolidated.read_text())
        require(type(value.get("zarr_consolidated_format")) is int and value["zarr_consolidated_format"] == 1
                and isinstance(value.get("metadata"), dict), f"Malformed consolidated NWB metadata: {path.name}")
        direct = {p.relative_to(path).as_posix(): json.loads(p.read_text())
                  for p in path.rglob("*") if p.is_file() and p.name in {".zarray", ".zattrs", ".zgroup"}}
        require(encoded(typed(direct)) == encoded(typed(value["metadata"])),
                f"Consolidated NWB metadata differs from physical metadata: {path.name}")
    return {"format": "zarr", "consolidated_metadata": consolidated.exists(), "nodes": nodes}


def hdf5_snapshot(path):
    import h5py

    nodes, seen = {}, {}
    with h5py.File(path, "r") as handle:
        require("nwb_version" in handle.attrs, f"Derived export is not NWB HDF5: {path.name}")

        def reference(value):
            if not isinstance(value, h5py.Reference):
                return None
            require(not isinstance(value, h5py.RegionReference),
                    f"Unsupported derived NWB region reference: {path.name}")
            if not value:
                return {"kind": "hdf5_object", "target": None}
            target = handle[value]
            return {"kind": "hdf5_object", "path": target.name,
                    "target_object_id": typed(target.attrs.get("object_id")),
                    "root_object_id": typed(handle.attrs.get("object_id"))}

        def attrs(obj):
            return {key: {"hdf5_type": obj.attrs.get_id(key).get_type().encode().hex(),
                          "value": typed(obj.attrs[key], reference)} for key in sorted(obj.attrs)}

        def visit(name, obj):
            address = h5py.h5o.get_info(obj.id).addr
            if address in seen:
                nodes[name] = {"kind": "hard_link", "target": seen[address]}
                return
            seen[address] = name
            node = {"kind": "dataset" if isinstance(obj, h5py.Dataset) else "group", "attrs": attrs(obj)}
            if isinstance(obj, h5py.Dataset):
                require(not obj.is_virtual and not obj.external,
                        f"Derived NWB data must be self-contained: {path.name}:{name}")
                node.update(data=array_identity(obj, reference), hdf5_type=obj.id.get_type().encode().hex())
            nodes[name] = node
            if isinstance(obj, h5py.Group):
                for child in sorted(obj):
                    target_name = name.rstrip("/") + "/" + child
                    link = obj.get(child, getlink=True)
                    require(not isinstance(link, h5py.ExternalLink),
                            f"Derived NWB link is external: {path.name}:{target_name}")
                    if isinstance(link, h5py.SoftLink):
                        target = obj[child]
                        nodes[target_name] = {"kind": "soft_link", "authored_target": link.path,
                                              "target_object_id": typed(target.attrs.get("object_id"))}
                    else:
                        visit(target_name, obj[child])

        visit("/", handle)
    return {"format": "hdf5", "nodes": nodes}


def capture(data):
    paths = sorted((data / "derived-nwb").glob("*.nwb*"))
    require(paths, "Final export produced no derived NWB")
    outputs = []
    for path in paths:
        require(not path.is_symlink(), f"Derived NWB must be a physical store: {path.name}")
        snapshot = zarr_snapshot(path) if path.is_dir() else hdf5_snapshot(path)
        outputs.append(dict(path=path.relative_to(data).as_posix(), **snapshot))
    return {"schema_version": 1, "outputs": outputs}


def verify(data, record):
    expected = record.get("metadata", {}).get("derived_science")
    require(isinstance(expected, dict) and type(expected.get("schema_version")) is int
            and expected["schema_version"] == 1 and set(expected) == {"schema_version", "outputs"}
            and isinstance(expected["outputs"], list), "Missing or malformed derived scientific receipt")
    actual = capture(data)
    require(len(expected["outputs"]) == len(actual["outputs"]), "Derived NWB output inventory differs from receipt")
    for wanted, observed in zip(expected["outputs"], actual["outputs"]):
        require(isinstance(wanted, dict) and set(wanted) == set(observed)
                and wanted.get("path") == observed["path"] and wanted.get("format") == observed["format"],
                "Derived NWB output identity differs from receipt")
        require(encoded(wanted.get("consolidated_metadata")) == encoded(observed.get("consolidated_metadata")),
                f"Derived NWB consolidated metadata presence differs: {observed['path']}")
        require(isinstance(wanted.get("nodes"), dict) and set(wanted["nodes"]) == set(observed["nodes"]),
                f"Derived NWB scientific node inventory differs: {observed['path']}")
        for name, node in observed["nodes"].items():
            original = wanted["nodes"][name]
            require(isinstance(original, dict) and set(original) == set(node),
                    f"Derived NWB scientific node structure differs: {observed['path']}:{name}")
            for field, value in node.items():
                require(encoded(original[field]) == encoded(value),
                        f"Derived NWB scientific {field} differs from receipt: {observed['path']}:{name}")
