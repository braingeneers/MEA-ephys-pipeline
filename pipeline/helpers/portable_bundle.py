#!/usr/bin/env python3
"""Assemble and verify a local scientific bundle from explicitly declared payloads.

Run in the pinned SpikeInterface scientific image. This is POSIX packaging, not
S3 transport. Historical provenance is retained; only declared executable
extractor graphs have their filesystem references rewritten.
"""
import argparse
import base64
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import pickle
import re
import shutil


CONTRACT = "mea.local-stage-bundle/v2"
BUNDLE_SCHEMA = 2
# This is part of the scientific receipt, not a tunable sampling parameter.
RECORDING_BLOCK_FRAMES = 65536
STAGES = {"nwb", "prepared", "sorted", "analyzed", "reported", "exported"}
FORMATS = {"extractor_json", "extractor_pickle", "analyzer_zarr"}
SERIALIZATION_SUFFIXES = {".json", ".pkl", ".pickle"}
FILE_KEYS = {"file_path", "file_paths", "folder_path", "folder_metadata"}


class BundleError(ValueError):
    pass


def jsonable(value):
    import numpy as np
    import pandas as pd

    if value is pd.NA:
        return {"pandas_na": True}
    if value is pd.NaT:
        return {"pandas_nat": True}
    if isinstance(value, np.ndarray):
        return jsonable(value.tolist())
    if isinstance(value, np.generic):
        return jsonable(value.item())
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes_base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite_float": str(value)}
    return value


def encoded(value):
    return json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def serialized_json(value):
    # Capsule consumers iterate dictionaries to choose plot rows. Canonical key
    # sorting belongs to manifest hashes, never executable serialization payloads.
    return json.dumps(jsonable(value), separators=(",", ":"), allow_nan=False).encode()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_serialization(path):
    # These are workflow-produced capsule dictionaries, including the original
    # non-JSON fallback; a bundle is not a generic untrusted-pickle ingestion API.
    return pickle.loads(path.read_bytes()) if path.suffix in {".pkl", ".pickle"} else json.loads(path.read_text())


def write_serialization(path, value):
    path.write_bytes(pickle.dumps(value, protocol=4) if path.suffix in {".pkl", ".pickle"} else serialized_json(value) + b"\n")


def array_identity(value):
    import numpy as np

    array = np.asarray(value)
    content = encoded(array.tolist()) if array.dtype.hasobject or array.dtype.fields else array.tobytes(order="C")
    return {"shape": list(array.shape), "dtype": jsonable(array.dtype.descr) if array.dtype.fields else array.dtype.str,
            "sha256": hashlib.sha256(content).hexdigest()}


def relative_path(value):
    if not isinstance(value, str) or not value:
        raise BundleError("Bundle paths must be non-empty relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise BundleError(f"Unsafe bundle path: {value}")
    return Path(path)


def within(root, value):
    path = (root / relative_path(value)).resolve()
    if not path.is_relative_to(root.resolve()):
        raise BundleError(f"Bundle path escapes its root: {value}")
    return path


def inventory(root):
    if root.is_symlink() or not root.is_dir():
        raise BundleError("Bundle inventory root must be a real directory")
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BundleError(f"Bundle contains a symlink: {path.relative_to(root)}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
    return result


def copy_payload(source, target, mappings, files, ancestors=()):
    """Dereference declared payloads and freeze their exact dependency mapping."""
    source = Path(source)
    resolved = source.resolve(strict=True)
    previous = mappings.get(resolved)
    if previous is not None and previous != target:
        raise BundleError(f"Ambiguous dependency destination for {source}")
    mappings[resolved] = target
    if resolved.is_dir():
        if resolved in ancestors:
            raise BundleError(f"Symlink directory cycle in declared payload: {source}")
        if target.exists() and not target.is_dir():
            raise BundleError(f"Destination collision: {target}")
        target.mkdir(parents=True, exist_ok=True)
        for item in sorted(source.iterdir()):
            copy_payload(item, target / item.name, mappings, files, (*ancestors, resolved))
    elif resolved.is_file():
        if target.exists():
            raise BundleError(f"Destination collision: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved, target)
        digest = sha256(resolved)
        if target.stat().st_size != resolved.stat().st_size or sha256(target) != digest:
            raise BundleError(f"Copy verification failed: {source}")
        files[target] = digest
    else:
        raise BundleError(f"Unsupported payload type: {source}")


def at_pointer(value, pointer):
    try:
        for key in pointer:
            value = value[key]
    except (KeyError, IndexError, TypeError) as error:
        raise BundleError(f"Missing serialized extractor at {pointer}") from error
    return value


def register_alias(source, target, mappings):
    """Bind an explicitly declared input copy to verified existing bundle bytes.

    Runtime JSON may have been reserialized by an earlier bundle. Only JSON
    formatting may differ; key order, every value and other bytes must match.
    This supports copy staging without associating dependencies by basename alone.
    """
    source = Path(source).resolve(strict=True)
    previous = mappings.get(source)
    if previous is not None:
        if previous != target:
            raise BundleError(f"Ambiguous dependency alias: {source}")
        return
    if source.is_dir():
        if not target.is_dir() or {p.name for p in source.iterdir()} != {p.name for p in target.iterdir()}:
            raise BundleError(f"Dependency alias directory differs: {source}")
        for item in sorted(source.iterdir()):
            register_alias(item, target / item.name, mappings)
    else:
        if not target.is_file():
            raise BundleError(f"Dependency alias lacks target: {source}")
        if sha256(source) != sha256(target):
            if source.suffix != '.json' or serialized_json(json.loads(source.read_text())) != serialized_json(json.loads(target.read_text())):
                raise BundleError(f"Dependency alias content differs: {source}")
    mappings[source] = target


def rebase_extractor(value, old_base, new_base, mappings, bundle_root, *, require_relative=False):
    """Rebase executable kwargs, leaving annotations and provenance untouched.

    A root relative_paths flag applies to every nested extractor, as in the pinned
    BaseExtractor.from_dict. NWB electrical_series_path is an HDF5 path, not a file.
    """
    if (not isinstance(value, dict) or not isinstance(value.get("class"), str)
            or not isinstance(value.get("kwargs"), dict) or not isinstance(value.get("relative_paths", False), bool)):
        raise BundleError("Malformed extractor dictionary or relative_paths flag")
    result = deepcopy(value)
    relative = value.get("relative_paths", False)

    def dependency(path):
        if not isinstance(path, (str, Path)) or not str(path):
            raise BundleError("Extractor filesystem dependency must be a non-empty string")
        original = Path(path)
        if require_relative and original.is_absolute():
            raise BundleError(f"Portable extractor requires relative filesystem paths: {path}")
        if relative:
            original = old_base / original
        elif not original.is_absolute():
            raise BundleError(f"Non-absolute dependency without relative_paths: {path}")
        try:
            # A producer may serialize relative to its future publication base.
            # Normalize that declared base before requiring the final dependency.
            original = original.resolve().resolve(strict=True)
        except FileNotFoundError as error:
            raise BundleError(f"Missing recording dependency: {original}") from error
        target = mappings.get(original)
        if target is None:
            raise BundleError(f"Undeclared recording dependency: {original}")
        if not target.resolve().is_relative_to((bundle_root / "data").resolve()):
            raise BundleError(f"Recording dependency escapes bundle data: {target}")
        return os.path.relpath(target, new_base)

    def visit(node):
        if isinstance(node, list):
            for child in node:
                visit(child)
            return
        if not isinstance(node, dict):
            return
        if "class" not in node:
            for child in node.values():
                visit(child)
            return
        if not isinstance(node.get("class"), str) or not node["class"].startswith("spikeinterface."):
            raise BundleError(f"Unsupported extractor class: {node.get('class')}")
        kwargs = node.get("kwargs")
        if not isinstance(kwargs, dict):
            raise BundleError("Malformed extractor kwargs")
        for key, item in list(kwargs.items()):
            if key in FILE_KEYS and item is not None:
                kwargs[key] = [dependency(path) for path in item] if isinstance(item, list) else dependency(item)
            elif key.endswith(("_path", "_paths")) and item is not None and key not in {"electrical_series_path"}:
                raise BundleError(f"Unsupported executable path field: {key}")
            else:
                visit(item)
        if node.get("folder_metadata") is not None:
            node["folder_metadata"] = dependency(node["folder_metadata"])

    visit(result)
    result["relative_paths"] = True
    return result


def assert_local_extractor(value, base, root):
    """Check active paths before SpikeInterface can silently discard a recording."""
    if not isinstance(value, dict) or value.get("relative_paths") is not True:
        raise BundleError("Portable extractor must declare relative_paths=true")
    mappings = {path.resolve(): path.resolve() for path in (root / "data").rglob("*")}
    mappings[(root / "data").resolve()] = (root / "data").resolve()
    rebase_extractor(value, base, base, mappings, root, require_relative=True)


def recording_identity(recording):
    import numpy as np

    result = {"sampling_frequency": recording.get_sampling_frequency(),
              "dtype": str(recording.get_dtype()), "channel_ids": jsonable(recording.channel_ids),
              "properties": {key: array_identity(recording.get_property(key))
                             for key in sorted(recording.get_property_keys())},
              "read_policy": {"algorithm": "mea.recording-stream/v1", "frames_per_block": RECORDING_BLOCK_FRAMES},
              "segments": []}
    for segment in range(recording.get_num_segments()):
        frames = recording.get_num_samples(segment)
        shape = (frames, recording.get_num_channels())
        def traces(start, end, scaled=False):
            return recording.get_traces(segment_index=segment, start_frame=start, end_frame=end, return_scaled=scaled)

        # get_times constructs the entire regular vector. Use the pinned SI
        # equation on global indices instead; never reconstruct explicit clocks.
        clock = recording.get_time_info(segment_index=segment)
        vector = clock["time_vector"]
        if vector is not None and tuple(vector.shape) != (frames,):
            raise BundleError("Recording time vector differs from segment frame count")
        def times(start, end):
            if vector is not None:
                return np.asarray(vector[start:end])
            values = np.arange(start, end, dtype="float64")
            values /= clock["sampling_frequency"]
            if clock["t_start"] is not None:
                values += clock["t_start"]
            return values

        result["segments"].append({"frames": frames,
            "raw": streamed_array_identity(shape, traces),
            "microvolts": streamed_array_identity(shape, lambda a, b: traces(a, b, True)),
            "times": streamed_array_identity((frames,), times)})
    return result


def streamed_array_identity(shape, read):
    """Hash every ordered value with bounded reads and exact dtype/shape checks."""
    import numpy as np

    digest, dtype = hashlib.sha256(), None
    # Read an empty array once as well: it still has a meaningful dtype/shape.
    for start in range(0, max(1, shape[0]), RECORDING_BLOCK_FRAMES):
        end = min(shape[0], start + RECORDING_BLOCK_FRAMES)
        array = np.asarray(read(start, end))
        if array.shape != (end - start, *shape[1:]) or array.dtype.hasobject or array.dtype.fields:
            raise BundleError("Recording stream must contain numeric arrays of the declared shape")
        if dtype is not None and array.dtype != dtype:
            raise BundleError("Recording stream dtype changed between blocks")
        dtype = array.dtype
        digest.update(array.tobytes(order="C"))
    return {"shape": list(shape), "dtype": dtype.str, "sha256": digest.hexdigest()}


def executable_graph(descriptor, root):
    """Read the declared executable graph without constructing an extractor."""
    location = within(root, descriptor["path"])
    base = within(root, descriptor.get("relative_to", "data"))
    if descriptor["format"] == "analyzer_zarr":
        import zarr
        return zarr.open(str(location), mode="r")["recording"][0], location
    return at_pointer(read_serialization(location), descriptor.get("pointer", [])), base


def verify_executables(root, record):
    """Preflight portable paths before loaders can use a machine-specific graph."""
    root = Path(root).resolve()
    for descriptor in record["serializations"]:
        value, base = executable_graph(descriptor, root)
        assert_local_extractor(value, base, root)


def executable_binding(descriptor, root):
    """Freeze active graph structure and complete dependencies in logical paths.

    Ordinary transport inventories may be resealed by a receiver. This separate
    producer receipt binds executable sources, selectors and ordering. Historical
    annotations are deliberately not executable. Compute after all graph rewrites.
    """
    import numpy as np

    value, base = executable_graph(descriptor, root)
    assert_local_extractor(value, base, root)
    dependencies = {}

    def dependency(item):
        if isinstance(item, list):
            return [dependency(path) for path in item]
        path = (base / item).resolve(strict=True)
        logical = path.relative_to(root.resolve()).as_posix()
        if path.is_dir():
            identity = {"kind": "directory", "files": inventory(path)}
        else:
            identity = {"kind": "file", "bytes": path.stat().st_size, "sha256": sha256(path)}
        dependencies[logical] = identity
        return {"bundle_path": logical}

    def normalized(node):
        if isinstance(node, np.ndarray):
            return {"numpy_array": array_identity(node)}
        if isinstance(node, np.generic):
            return {"numpy_scalar": {"dtype": node.dtype.str, "value": jsonable(node.item())}}
        if isinstance(node, tuple):
            return {"tuple": [normalized(item) for item in node]}
        if isinstance(node, list):
            return [normalized(item) for item in node]
        if not isinstance(node, dict):
            return jsonable(node)
        if "class" not in node:
            return {"mapping": [[normalized(key), normalized(item)] for key, item in node.items()]}
        # Preserve meaningful key order in kwargs; omit non-executable history.
        kwargs = [[key, dependency(item) if key in FILE_KEYS and item is not None else normalized(item)]
                  for key, item in node["kwargs"].items()]
        fields = [[key, normalized(node[key])] for key in ("class", "module", "version", "relative_paths") if key in node]
        fields.append(["kwargs", {"mapping": kwargs}])
        if node.get("folder_metadata") is not None:
            fields.append(["folder_metadata", dependency(node["folder_metadata"])])
        return {"extractor": fields}

    graph = normalized(value)
    return {"graph": graph, "dependencies": dependencies}


def sorting_identity(sorting):
    return {"sampling_frequency": sorting.get_sampling_frequency(), "unit_ids": jsonable(sorting.unit_ids),
            "segments": sorting.get_num_segments(), "spikes": array_identity(sorting.to_spike_vector())}


def fingerprint(descriptor, root, portable=False):
    import spikeinterface as si

    location = within(root, descriptor["path"]) if portable else Path(descriptor["source"])
    base = within(root, descriptor.get("relative_to", "data")) if portable else Path(descriptor["base"])
    if descriptor["format"] == "analyzer_zarr":
        import zarr
        group = zarr.open(str(location), mode="r")
        if "recording" not in group:
            raise BundleError(f"Analyzer lacks a serialized recording: {location}")
        value = group["recording"][0]
        if portable:
            assert_local_extractor(value, location, root)
        recording = si.load(value, base_folder=base)
        analyzer = si.SortingAnalyzer.load_from_zarr(location, recording=recording)
        if analyzer.recording is None:
            raise BundleError(f"Analyzer recording could not be loaded: {location}")
        extensions = {}
        for name in ("quality_metrics", "template_metrics", "templates", "unit_locations"):
            if analyzer.has_extension(name):
                data = analyzer.get_extension(name).get_data()
                if hasattr(data, "to_numpy"):
                    extensions[name] = {"values": array_identity(data.to_numpy()),
                                        "index": jsonable(data.index.tolist()), "columns": list(data.columns)}
                else:
                    extensions[name] = array_identity(data)
        return {"recording": recording_identity(analyzer.recording), "sorting": sorting_identity(analyzer.sorting),
                "extension_data": extensions,
                "extension_files": inventory(location / "extensions") if (location / "extensions").is_dir() else {}}
    value = at_pointer(read_serialization(location), descriptor.get("pointer", []))
    if portable:
        assert_local_extractor(value, base, root)
    extractor = si.load(value, base_folder=base)
    if isinstance(extractor, si.BaseRecording):
        return {"recording": recording_identity(extractor)}
    if isinstance(extractor, si.BaseSorting):
        return {"sorting": sorting_identity(extractor)}
    raise BundleError(f"Unsupported executable extractor type: {location}")


def required_serializations(data):
    """Known executable capsule outputs must never be omitted from the catalogue."""
    required = set()
    for path in data.rglob("*"):
        relative = path.relative_to(data.parent).as_posix()
        if path.is_file() and path.name in {"si_folder.json", "binary.json"}:
            required.add((relative, ()))
        if path.parent == data and path.suffix in SERIALIZATION_SUFFIXES:
            if path.name.startswith(("binary_", "preprocessed_", "motioncorrected_")):
                required.add((relative, ()))
            elif path.name.startswith("job_"):
                required.add((relative, ("recording_dict",)))
            elif path.name.startswith("preprocessedviz_"):
                value = read_serialization(path)
                for name, content in value.items():
                    required.add((relative, (name, "drift", "recording")))
                    for mode in ("full", "proc"):
                        for layer in (content["timeseries"][mode] or {}):
                            required.add((relative, (name, "timeseries", mode, layer)))
        if path.is_file() and path.name == ".zattrs":
            attributes = json.loads(path.read_text())
            if "spikeinterface_info" in attributes and "settings" in attributes:
                required.add((path.parent.relative_to(data.parent).as_posix(), ()))
    return required


def check_catalogue(descriptors, data):
    if not isinstance(descriptors, list):
        raise BundleError("Serialization catalogue must be a list")
    for item in descriptors:
        if not isinstance(item, dict) or item.get("format") not in FORMATS:
            raise BundleError("Unknown executable serialization format")
        path = relative_path(item["path"])
        if path.parts[0] != "data":
            raise BundleError("Executable serialization must be under data/")
        relative_path(item.get("relative_to", "data"))
        if not isinstance(item.get("pointer", []), list):
            raise BundleError("Serialization pointer must be a list")
        suffixes = {".json"} if item["format"] == "extractor_json" else {".pkl", ".pickle"}
        if item["format"] != "analyzer_zarr" and path.suffix not in suffixes:
            raise BundleError("Serialization filename and format disagree")
        if item["format"] == "analyzer_zarr" and item.get("pointer"):
            raise BundleError("Analyzer recording descriptors cannot have JSON pointers")
    actual = [(item["path"], tuple(item.get("pointer", []))) for item in descriptors]
    if len(set(actual)) != len(actual):
        raise BundleError("Duplicate executable serialization descriptor")
    missing = required_serializations(data) - set(actual)
    if missing:
        raise BundleError(f"Executable serialization missing from catalogue: {sorted(missing)}")


def capsule_serializations(source, destination, data_base):
    """Describe unchanged capsule output formats using explicitly supplied bases.

    source is a declared output directory, destination is its portable data path,
    and data_base is that invocation's declared capsule input layout. No producing
    task directory is inferred from an artifact filename.
    """
    source, destination, data_base = Path(source), Path(destination), Path(data_base)
    result = []

    def add(path, base, pointer=(), relative_to=None, kind="extractor_json"):
        result.append({"format": "extractor_pickle" if kind == "extractor_json" and path.suffix in {".pkl", ".pickle"} else kind,
                       "source": str(path), "path": str(destination / path.relative_to(source)),
                       "base": str(base), "pointer": list(pointer),
                       "relative_to": str(destination if relative_to is None else relative_to)})

    for path in sorted(source.rglob("*")):
        if path.is_file() and path.name in {"si_folder.json", "binary.json"}:
            add(path, path.parent, relative_to=destination / path.parent.relative_to(source))
        if path.parent == source and path.suffix in SERIALIZATION_SUFFIXES:
            if path.name.startswith(("binary_", "preprocessed_", "motioncorrected_")):
                add(path, source)
            elif path.name.startswith("job_"):
                add(path, data_base, ("recording_dict",))
                if read_serialization(path).get("recording_lfp_dict") is not None:
                    add(path, data_base, ("recording_lfp_dict",))
            elif path.name.startswith("preprocessedviz_"):
                for recording, content in read_serialization(path).items():
                    skipped = (source / ("preprocessed_" + recording) / "error.txt").exists()
                    add(path, data_base if skipped else source, (recording, "drift", "recording"))
                    for mode in ("full", "proc"):
                        for layer in (content["timeseries"][mode] or {}):
                            add(path, data_base, (recording, "timeseries", mode, layer))
        if path.is_file() and path.name == ".zattrs":
            value = json.loads(path.read_text())
            if "spikeinterface_info" in value and "settings" in value:
                add(path.parent, path.parent, relative_to=destination / path.parent.relative_to(source), kind="analyzer_zarr")
    return result


def check_recording_ids(record, data):
    ids = record.get("recording_ids")
    if (not isinstance(ids, list) or not ids or not all(isinstance(item, str) and item for item in ids)
            or len(set(ids)) != len(ids)):
        raise BundleError("recording_ids must contain unique non-empty recording identities")
    jobs = [read_serialization(path).get("recording_name") for path in sorted(data.glob("job_*"))
            if path.suffix in SERIALIZATION_SUFFIXES]
    if record["stage"] != "nwb" and (len(jobs) != len(ids) or set(jobs) != set(ids)):
        raise BundleError("Declared recording identities do not match the dispatch inventory")


def pack(spec, output):
    if type(spec.get("schema_version")) is not int or spec["schema_version"] != 1 or spec.get("stage") not in STAGES:
        raise BundleError("Unsupported bundle assembly schema or stage")
    unit = spec.get("unit_id")
    if not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", unit):
        raise BundleError("unit_id must be a stable non-empty path-safe identity")
    output = Path(output).absolute()
    for item in spec["payloads"]:
        source = Path(item["source"]).resolve(strict=True)
        if source.is_dir() and output.resolve().is_relative_to(source):
            raise BundleError("Bundle output cannot be inside a declared source directory")
    output.mkdir(parents=True, exist_ok=False)
    mappings, copied = {}, {}
    for item in spec["payloads"]:
        destination = relative_path(item["destination"])
        if destination.parts[0] != "data":
            raise BundleError("Payload destinations must be under data/")
        copy_payload(item["source"], output / destination, mappings, copied)
    for item in spec.get('dependency_aliases', []):
        destination = relative_path(item['destination'])
        if destination.parts[0] != 'data':
            raise BundleError('Dependency alias destinations must be under data/')
        register_alias(item['source'], output / destination, mappings)
    descriptors = spec.get("serializations", [])
    check_recording_ids(spec, output / "data")
    check_catalogue(descriptors, output / "data")
    science, original_serializations = [], []
    for descriptor in descriptors:
        if descriptor.get("format") not in FORMATS:
            raise BundleError("Unknown executable serialization format")
        destination = within(output, descriptor["path"])
        original = Path(descriptor["source"]).resolve(strict=True)
        if descriptor["format"] != "analyzer_zarr" and original.suffix != destination.suffix:
            raise BundleError("Source and destination serialization formats disagree")
        if mappings.get(original) != destination:
            raise BundleError(f"Serialization is not bound to its declared payload: {original}")
        if descriptor["format"] == "analyzer_zarr":
            import zarr
            group = zarr.open(str(destination), mode="r+")
            source_group = zarr.open(str(original), mode="r")
            value = source_group["recording"][0]
            replacement = rebase_extractor(value, Path(descriptor["base"]), destination, mappings, output)
            expected = fingerprint(descriptor, output)
            group["recording"][0] = replacement
            zarr.consolidate_metadata(group.store)
        else:
            value = read_serialization(destination)
            pointer = descriptor.get("pointer", [])
            extractor = at_pointer(value, pointer)
            replacement = rebase_extractor(extractor, Path(descriptor["base"]),
                                           within(output, descriptor.get("relative_to", "data")), mappings, output)
            expected = fingerprint(descriptor, output)
            if pointer:
                at_pointer(value, pointer[:-1])[pointer[-1]] = replacement
            else:
                value = replacement
            write_serialization(destination, value)
        portable_descriptor = {key: value for key, value in descriptor.items() if key not in {"source", "base"}}
        if encoded(fingerprint(portable_descriptor, output, portable=True)) != encoded(expected):
            raise BundleError(f"Scientific identity changed during packaging: {descriptor['path']}")
        science.append(expected)
        original_serializations.append({"path": descriptor["path"], "pointer": descriptor.get("pointer", []),
                                        "sha256": sha256(original) if original.is_file() else None})
    payload_inventory = inventory(output / "data")
    record = {"schema_version": BUNDLE_SCHEMA, "contract": CONTRACT, "unit_id": unit, "stage": spec["stage"],
              "recording_ids": spec["recording_ids"], "metadata": spec.get("metadata", {}),
              "files": payload_inventory, "bytes": sum(item["bytes"] for item in payload_inventory.values()),
              "serializations": [{key: value for key, value in item.items() if key not in {"source", "base"}}
                                 for item in descriptors], "scientific_fingerprints": science,
              "executable_bindings": [executable_binding(item, output) for item in descriptors],
              "original_serializations": original_serializations,
              "historical_provenance_paths": "Retained as evidence; not executable dependencies."}
    record["content_sha256"] = hashlib.sha256(encoded(record)).hexdigest()
    (output / "bundle.json").write_bytes(encoded(record) + b"\n")
    return record


def verify_manifest(root):
    """Check transport and declared structure without loading scientific arrays."""
    root = Path(root).resolve()
    record = json.loads((root / "bundle.json").read_text())
    if type(record.get("schema_version")) is not int:
        raise BundleError("Bundle schema_version must be exact integer 2")
    if record["schema_version"] == 1:
        raise BundleError("Legacy scientific bundle v1 lacks complete recording receipts; re-run its named stage producer "
                          "to create v2. Audit historical v1 artifacts with their frozen original verifier.")
    if record["schema_version"] != BUNDLE_SCHEMA or record.get("contract") != CONTRACT or record.get("stage") not in STAGES:
        raise BundleError("Unsupported scientific bundle contract (expected mea.local-stage-bundle/v2)")
    unsigned = {key: value for key, value in record.items() if key != "content_sha256"}
    if hashlib.sha256(encoded(unsigned)).hexdigest() != record.get("content_sha256"):
        raise BundleError("Bundle manifest digest mismatch")
    actual = inventory(root / "data")
    if encoded(actual) != encoded(record["files"]):
        raise BundleError("Bundle file inventory mismatch (missing, changed or unexpected payload)")
    if type(record.get("bytes")) is not int or record["bytes"] != sum(item["bytes"] for item in actual.values()):
        raise BundleError("Bundle byte count differs from its complete inventory")
    check_catalogue(record["serializations"], root / "data")
    check_recording_ids(record, root / "data")
    return record


def verify_science(root, record):
    """Check immutable producer receipts after stage metadata/policy preflight."""
    root = Path(root).resolve()
    identities = [fingerprint(item, root, portable=True) for item in record["serializations"]]
    if encoded(identities) != encoded(record["scientific_fingerprints"]):
        raise BundleError("Bundle scientific fingerprint mismatch")
    bindings = [executable_binding(item, root) for item in record["serializations"]]
    if encoded(bindings) != encoded(record.get("executable_bindings")):
        raise BundleError("Bundle executable binding mismatch")
    return {"passed": True, "unit_id": record["unit_id"], "stage": record["stage"], "bytes": record["bytes"],
            "files": len(record["files"]), "serializations": len(identities), "content_sha256": record["content_sha256"]}


def verify(root):
    """Contents-only API; stage consumers also enforce invocation/native/work policy."""
    record = verify_manifest(root)
    verify_executables(root, record)
    return verify_science(root, record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("pack")
    create.add_argument("--spec", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = pack(json.loads(args.spec.read_text()), args.output) if args.command == "pack" else verify(args.bundle)
    except (BundleError, FileNotFoundError) as error:
        parser.exit(1, f"Scientific bundle error: {error}\n")
    print(json.dumps({key: result[key] for key in ("unit_id", "stage", "bytes", "content_sha256")}, indent=2))


if __name__ == "__main__":
    main()
