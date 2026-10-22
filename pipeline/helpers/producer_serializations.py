"""Make owned executable output metadata usable after Nextflow stage-out.

No numerical payload is read or changed. The capsule's live coordinate system
and declared input links bind dependencies before its scratch directory retires.
The ordinary bundle packer still performs complete scientific verification.
"""
import json
import os
from pathlib import Path
import pickle
import stat
import uuid

import portable_bundle as portable


def require(condition, message):
    if not condition:
        raise portable.BundleError("Producer serialization: " + message)


def owned_parent(path, results):
    path = Path(path)
    require(path.parent.resolve(strict=True).is_relative_to(results),
            "metadata parent escapes owned results: " + str(path))
    return path


def replace_bytes(path, content, results):
    """Replace an owned name, without modifying a linked source inode."""
    path = owned_parent(path, results)
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def absolute_modes(value):
    """All rewritten executable paths are absolute, including nested objects."""
    if isinstance(value, list):
        for item in value:
            absolute_modes(item)
    elif isinstance(value, dict) and 'class' in value:
        portable.extractor_relative(value)
        if 'relative_paths' in value:
            value['relative_paths'] = False
        for item in value['kwargs'].values():
            absolute_modes(item)
    elif isinstance(value, dict):
        for item in value.values():
            absolute_modes(item)


def normalize(producer, published, published_results, role, links, context=None):
    producer = Path(producer).resolve(strict=True)
    results = (producer / 'results').resolve(strict=True)
    require(results.is_relative_to(producer), 'results escape the producing capsule')
    data = (producer / 'data').resolve(strict=True)
    published, published_results = Path(published), Path(published_results)
    context = context or {}
    require(set(context) <= {'input_base', 'publication_base'}, 'unknown serialization context')

    def context_base(name):
        value = context.get(name)
        require(isinstance(value, str) and value, 'collector needs its declared ' + name)
        # The unchanged capsule evaluates its CLI paths from capsule/code.
        # Preserve that coordinate system for relative direct-call arguments.
        return (producer / 'code' / value).resolve()

    input_targets = [Path(row['target']).resolve(strict=True) for row in links
                     if Path(row['source']).is_relative_to(producer / 'data')]
    input_targets = [(path, path.is_dir()) for path in input_targets]
    descriptors = portable.capsule_serializations(results, 'data', producer / 'data')

    def dependency(raw, base, relative):
        require(isinstance(raw, (str, Path)) and str(raw), 'empty dependency')
        path = Path(raw)
        require(path.is_absolute() or relative, 'relative dependency without its path-mode flag')
        # Resolve live symlinks before '..'. A declared future publication base
        # need not exist, but the final source must exist and belong to this task.
        source = ((Path(base) / path) if relative else path).resolve().resolve(strict=True)
        if source.is_relative_to(results):
            return str(published_results / source.relative_to(results))
        if source.is_relative_to(data):
            return str(published / 'data' / source.relative_to(data))
        require(not source.is_relative_to(producer), 'dependency is not an emitted input/output: ' + str(source))
        require(any(source == root or (directory and source.is_relative_to(root))
                    for root, directory in input_targets), 'dependency is not a declared staged input: ' + str(source))
        return str(source)

    def graph(value, base):
        relative = portable.extractor_relative(value)
        result = portable.map_extractor_dependencies(value,
            lambda raw: dependency(raw, base, relative), False)
        absolute_modes(result)
        return result

    # Multiple graph pointers can share one JSON/pickle document. Preserve their
    # insertion order and every non-path value, and replace each document once.
    documents = {}
    rewritten = []
    for descriptor in descriptors:
        path = Path(descriptor['source'])
        base = Path(descriptor['base'])
        if role == 'results_collector' and descriptor['format'] == 'analyzer_zarr':
            base = context_base('publication_base') / 'postprocessed' / path.name
        if descriptor['format'] == 'analyzer_zarr':
            import zarr
            require(path.resolve(strict=True).is_relative_to(results), 'analyzer escapes owned results')
            group = zarr.open(str(path), mode='a')
            require('recording' in group and group['recording'].shape == (1,), 'malformed analyzer recording')
            recording = path / 'recording'
            require(recording.resolve(strict=True).is_relative_to(results), 'recording metadata escapes owned results')
            # DirectoryStore normally replaces chunks atomically. Break any
            # metadata hard links first as an additional source-protection bound.
            for child in recording.rglob('*'):
                if child.is_file():
                    owned_parent(child, results)
                    require(not child.is_symlink(), 'linked analyzer recording metadata')
                    if child.stat().st_nlink > 1:
                        replace_bytes(child, child.read_bytes(), results)
            group['recording'][0] = graph(group['recording'][0], base)
        else:
            owned_parent(path, results)
            if path.name == 'binary.json':
                # BinaryFolderRecording requires this representation's flag to
                # stay true. Its native data files move with the folder, so no
                # coordinate changes are needed (or permitted) in this sidecar.
                value = portable.read_serialization(path)
                require(portable.extractor_relative(value), 'binary-folder sidecar must remain relative')

                def colocated(raw):
                    require(isinstance(raw, (str, Path)) and not Path(raw).is_absolute(),
                            'binary-folder data must be relative to its sidecar')
                    require((path.parent / raw).resolve(strict=True).is_relative_to(path.parent.resolve()),
                            'binary-folder data must stay with its sidecar')
                    return raw

                portable.map_extractor_dependencies(value, colocated, True)
                continue
            if path not in documents:
                documents[path] = portable.read_serialization(path)
            value = documents[path]
            pointer = descriptor.get('pointer', [])
            replacement = graph(portable.at_pointer(value, pointer), base)
            if pointer:
                parent = portable.at_pointer(value, pointer[:-1])
                parent[pointer[-1]] = replacement
            else:
                documents[path] = replacement
        rewritten.append(str(path.relative_to(results)))

    # Collector preprocessed JSON is serialized against its explicit pipeline
    # input base, rather than the collector output directory.
    if role == 'results_collector':
        for path in sorted((results / 'preprocessed').glob('*.json')):
            base = context_base('input_base')
            owned_parent(path, results)
            documents[path] = graph(portable.read_serialization(path), base)
            rewritten.append(str(path.relative_to(results)))

    for path, value in documents.items():
        # JSON input already has JSON scalar types. Avoid canonical hash
        # conversion (which has special scientific NaN/nullable encodings).
        content = (pickle.dumps(value, protocol=4) if path.suffix in {'.pkl', '.pickle'}
                   else (json.dumps(value, separators=(',', ':')) + '\n').encode())
        replace_bytes(path, content, results)
    return rewritten
