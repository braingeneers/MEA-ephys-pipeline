"""Find the exact source NWB declared by executable recording dictionaries."""
from pathlib import Path


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
