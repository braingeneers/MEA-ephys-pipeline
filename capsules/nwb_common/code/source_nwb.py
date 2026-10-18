"""Find the exact source NWB declared by executable recording dictionaries."""
from pathlib import Path


def _cached_nwb_versions(path):
    """Read version declarations before merging any cached schema into a map."""
    import json

    def collect(root):
        versions = [('core', root.attrs.get('nwb_version'))]
        location = root.attrs.get('.specloc')
        if location is None:
            return versions
        specs = root[location]
        for name in specs.keys():
            stored = specs[name]
            choices = list(stored.keys())
            if not choices:
                raise ValueError(f'Cached NWB namespace {name!r} has no version.')
            # Both pinned backends select the last stored version. Read its
            # declarations without resolving cross-namespace dependencies.
            raw = stored[choices[-1]]['namespace'][()]
            if hasattr(raw, 'size') and raw.size == 1:
                raw = raw.item()
            for record in json.loads(raw)['namespaces']:
                versions.append((record['name'], record.get('version')))
        return versions

    if Path(path).is_dir():
        import zarr

        root = zarr.open_group(str(path), mode='r')
        try:
            return collect(root)
        finally:
            close = getattr(root.store, 'close', None)
            if close is not None:
                close()
    import h5py

    with h5py.File(path, 'r') as root:
        return collect(root)


def open_source_nwb(path, mode, io_class, *, for_writing=False):
    """Open a source with an isolated map and explicit cached-schema checks.

    Older core files remain readable with the current core implementation.
    A newer cached core, or a different modeled extension version, must not be
    silently ignored in favor of installed classes before scientific export.
    """
    from hdmf.build import BuildManager
    from packaging.version import InvalidVersion, Version
    from pynwb import get_type_map

    # Writers stage the owning helper explicitly beside this adapter. Import it
    # before opening a writer; ordinary read-only metadata discovery stays usable.
    if for_writing or mode != 'r':
        from nwb_graph import require_writable_graph

    versions = _cached_nwb_versions(path)
    if any(name == 'ndx-cell-culture' for name, _ in versions):
        try:
            import ndx_cell_culture  # noqa: F401; register the installed schema.
        except ModuleNotFoundError as error:
            if error.name != 'ndx_cell_culture':
                raise
            raise ValueError('Modeled NWB input requires the pinned ndx-cell-culture runtime.') from error

    type_map = get_type_map()
    installed = type_map.namespace_catalog
    for name, source_version in versions:
        if name not in installed.namespaces:
            continue
        if isinstance(source_version, bytes):
            source_version = source_version.decode('utf-8')
        installed_version = installed.get_namespace(name).version
        try:
            source = Version(str(source_version))
            current = Version(str(installed_version))
        except InvalidVersion as error:
            raise ValueError(f'Cannot verify cached NWB namespace {name!r} version {source_version!r}.') from error
        if source > current or (name == 'ndx-cell-culture' and source != current):
            raise ValueError(f'Unsupported cached NWB namespace {name!r}: source {source_version}, '
                             f'installed {installed_version}. Use a compatible NWB I/O runtime.')
    # Merge into this invocation's copy, never the global registered type map.
    io_class.load_namespaces(type_map, str(path))
    reader = io_class(str(path), mode, manager=BuildManager(type_map))
    if for_writing or mode != 'r':
        try:
            require_writable_graph(reader)
        except BaseException:
            reader.close()
            raise
    return reader


def source_nwb_from_jobs(jobs, data_base):
    """Return one source header per session; never infer it from a basename.

    The outer relative_paths flag governs nested extractor kwargs, as in the
    pinned SpikeInterface loader. Historical annotations are not executable inputs.
    """
    sources = set()

    def visit(node, relative):
        if isinstance(node, list):
            for item in node:
                visit(item, relative)
        elif isinstance(node, dict):
            if 'class' in node:
                kwargs = node.get('kwargs', {})
                if node['class'].split('.')[-1] == 'NwbRecordingExtractor':
                    value = kwargs.get('file_path')
                    if not isinstance(value, (str, Path)) or not str(value):
                        raise ValueError('NWB recording lacks its declared file_path')
                    path = Path(value)
                    if relative:
                        path = Path(data_base) / path
                    elif not path.is_absolute():
                        raise ValueError('NWB recording path is relative without relative_paths')
                    path = path.resolve(strict=True)
                    if not path.is_file() and not (path.is_dir() and (path / '.zattrs').is_file()):
                        raise ValueError(f'Declared source is not an NWB file/store: {path}')
                    sources.add(path)
                visit(kwargs, relative)
            else:
                for item in node.values():
                    visit(item, relative)

    for job in jobs:
        graph = job['recording_dict']
        visit(graph, graph.get('relative_paths', False))
    if len(sources) > 1:
        raise ValueError('A session references multiple source NWBs; export separate metadata-owned units')
    return next(iter(sources)) if sources else None


def electrode_groups_for_recording(recording, nwbfile):
    """Resolve existing electrode rows by their explicit channel/group identity."""
    if nwbfile.electrodes is None:
        return None
    table = nwbfile.electrodes
    names = table['channel_name'][:] if 'channel_name' in table.colnames else table.id[:]
    groups = [str(value) for value in table['group_name'][:]]
    channel_names = recording.get_property('channel_name')
    if channel_names is None:
        channel_names = recording.channel_ids
    explicit_groups = recording.get_property('group_name')
    current_groups = recording.get_channel_groups()
    resolved = []
    for index, name in enumerate(channel_names):
        candidates = [row for row, value in enumerate(names) if str(value) == str(name)]
        if len(candidates) > 1:
            labels = explicit_groups if explicit_groups is not None else current_groups
            candidates = [row for row in candidates if groups[row] == str(labels[index])]
        if len(candidates) != 1:
            raise ValueError(f'Cannot bind channel {name!r} to one existing NWB electrode row')
        resolved.append(groups[candidates[0]])
    return resolved


def metadata_for_electrode_groups(nwbfile, group_names):
    groups = [nwbfile.electrode_groups[name] for name in dict.fromkeys(group_names)]
    return {'Ecephys': {
        'Device': [{'name': name} for name in dict.fromkeys(group.device.name for group in groups)],
        'ElectrodeGroup': [{'name': group.name, 'description': group.description,
                           'location': group.location, 'device': group.device.name} for group in groups]}}


def apply_electrode_groups(recording, group_names):
    import numpy as np

    names = np.asarray(group_names, dtype=str)
    recording.set_channel_groups(names)
    recording.set_property('group_name', names)


def devices_for_units(analyzer, recording, nwbfile, group_names):
    """Keep unit hardware labels tied to the source electrode table."""
    import numpy as np

    devices = [nwbfile.electrode_groups[name].device.name for name in group_names]
    if not analyzer.get_num_units():
        return np.asarray([], dtype=str)
    if len(set(devices)) == 1:
        return np.asarray([devices[0]] * analyzer.get_num_units(), dtype=str)
    from spikeinterface.core.template_tools import get_template_extremum_channel

    by_channel = dict(zip(recording.channel_ids, devices))
    peaks = get_template_extremum_channel(analyzer, peak_sign='both', outputs='id')
    return np.asarray([by_channel[peaks[unit]] for unit in analyzer.unit_ids], dtype=str)
