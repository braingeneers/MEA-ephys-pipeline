"""Helpers for Maxwell HDF5 raw dataset discovery and compatibility views."""

from __future__ import annotations

import os
from pathlib import Path
import re

import h5py
import numpy as np


DEFAULT_MAXWELL_RAW_DATASET_PATH = "/data_store/data0000/groups/routed/raw"
MAXWELL_HDF5_PLUGIN_FILENAME = "libcompression.so"
DEFAULT_MAXWELL_HDF5_PLUGIN_DIR = Path.home() / "hdf5_plugin_path_maxwell"
DATA_STORE_RAW_PATTERN = re.compile(r"^/data_store/data(\d+)/groups/routed/raw$")


def configure_hdf5_plugin_search_paths(paths: list[str]) -> None:
    """Register existing HDF5 plugin directories with the active runtime."""
    normalized_paths: list[str] = []
    for path in paths:
        if not path:
            continue
        candidate = str(Path(path).resolve())
        if Path(candidate).exists() and candidate not in normalized_paths:
            normalized_paths.append(candidate)

    if normalized_paths:
        os.environ["HDF5_PLUGIN_PATH"] = os.pathsep.join(normalized_paths)

    for index in reversed(range(h5py.h5pl.size())):
        configured = h5py.h5pl.get(index).decode()
        if not Path(configured).exists():
            h5py.h5pl.remove(index)

    active_paths = [h5py.h5pl.get(index).decode() for index in range(h5py.h5pl.size())]
    for path in reversed(normalized_paths):
        if path not in active_paths:
            h5py.h5pl.prepend(path.encode())


def configure_bundled_maxwell_hdf5_plugin() -> Path:
    """Configure a preinstalled Maxwell filter plugin without network access."""
    configured_paths = os.environ.get("HDF5_PLUGIN_PATH", "")
    candidates = [
        Path(raw_path)
        for raw_path in configured_paths.split(os.pathsep)
        if raw_path
    ]
    candidates.append(DEFAULT_MAXWELL_HDF5_PLUGIN_DIR)

    checked: list[str] = []
    for directory in candidates:
        resolved_directory = directory.expanduser().resolve()
        plugin_file = resolved_directory / MAXWELL_HDF5_PLUGIN_FILENAME
        if str(plugin_file) in checked:
            continue
        checked.append(str(plugin_file))
        if plugin_file.is_file():
            configure_hdf5_plugin_search_paths([str(resolved_directory)])
            return resolved_directory

    raise RuntimeError(
        "Maxwell HDF5 filter 401 plugin is not bundled in this runtime image; "
        "runtime downloads are disabled. Checked: " + ", ".join(checked)
    )


def require_hdf5_filter_capabilities(
    filter_id: int,
    *,
    require_decode: bool = True,
    require_encode: bool = False,
) -> None:
    """Require the native HDF5 filter capabilities used by the requested operation."""
    if not h5py.h5z.filter_avail(filter_id):
        raise RuntimeError(
            f"Requested HDF5 filter id={filter_id} is unavailable. "
            "Confirm the plugin path is configured and the plugin is installed."
        )

    filter_info = h5py.h5z.get_filter_info(filter_id)
    if require_decode and not (filter_info & h5py.h5z.FILTER_CONFIG_DECODE_ENABLED):
        raise RuntimeError(
            f"HDF5 filter id={filter_id} is available but decode support is disabled. "
            "Cannot read the Maxwell source data."
        )
    if require_encode and not (filter_info & h5py.h5z.FILTER_CONFIG_ENCODE_ENABLED):
        raise RuntimeError(
            f"HDF5 filter id={filter_id} is available but encode support is disabled. "
            "Cannot write output with this codec."
        )


def normalize_h5_path(path: str) -> str:
    if not path:
        return path
    return path if path.startswith("/") else f"/{path}"


def list_routed_raw_dataset_paths(file_handle: h5py.File) -> list[str]:
    paths: list[str] = []
    if "data_store" not in file_handle:
        return paths

    for data_key in sorted(file_handle["data_store"].keys()):
        candidate = f"/data_store/{data_key}/groups/routed/raw"
        if candidate in file_handle:
            paths.append(candidate)
    return paths


def count_routed_raw_dataset_paths(input_file: Path) -> int:
    with h5py.File(input_file, "r") as file_handle:
        return len(list_routed_raw_dataset_paths(file_handle))


def infer_well_name_from_raw_dataset_path(raw_dataset_path: str) -> str | None:
    match = DATA_STORE_RAW_PATTERN.match(normalize_h5_path(raw_dataset_path))
    if not match:
        return None
    return f"well{int(match.group(1)):03d}"


def raw_dataset_path_for_well(file_handle: h5py.File, well_name: str) -> str:
    """Resolve a Max Two well through recording links before falling back to data-store numbering."""
    raw_paths = list_routed_raw_dataset_paths(file_handle)
    linked_group = None
    if "recordings" in file_handle:
        for recording in file_handle["recordings"].values():
            if well_name in recording:
                linked_group = recording[well_name]
                break
    if linked_group is not None:
        for raw_path in raw_paths:
            data_group_path = raw_path[: -len("/groups/routed/raw")]
            if file_handle[data_group_path].id == linked_group.id:
                return raw_path

    digits = well_name.removeprefix("well")
    if digits.isdigit():
        candidate = f"/data_store/data{int(digits):04d}/groups/routed/raw"
        if candidate in file_handle:
            return candidate
        for raw_path in raw_paths:
            data_group_path = raw_path[: -len("/groups/routed/raw")]
            well_id_path = f"{data_group_path}/well_id"
            if well_id_path in file_handle:
                value = file_handle[well_id_path][0]
                if int(value) == int(digits):
                    return raw_path
    raise ValueError(f"Could not resolve routed Maxwell raw dataset for {well_name}")


def discover_routed_raw_dataset_path(
    input_file: Path,
    *,
    requested_path: str = "",
    require_single: bool = True,
) -> str:
    requested_path = requested_path.strip() if requested_path else ""
    if requested_path and requested_path.lower() != "auto":
        if requested_path.lower().startswith("well:"):
            well_name = requested_path.split(":", 1)[1].strip().lower()
            if not re.fullmatch(r"well\d+", well_name):
                raise ValueError(f"Invalid Maxwell well selector: {requested_path}")
            with h5py.File(input_file, "r") as file_handle:
                return raw_dataset_path_for_well(file_handle, well_name)
        requested_path = normalize_h5_path(requested_path)
        with h5py.File(input_file, "r") as file_handle:
            if requested_path not in file_handle:
                raise ValueError(f"Requested Maxwell raw dataset path not found: {requested_path}")
        return requested_path

    with h5py.File(input_file, "r") as file_handle:
        raw_paths = list_routed_raw_dataset_paths(file_handle)

    if not raw_paths:
        raise ValueError("No routed Maxwell raw dataset found under /data_store/data*/groups/routed/raw")

    if require_single and len(raw_paths) != 1:
        raise ValueError(
            "Expected exactly one routed Maxwell raw dataset. "
            f"Found {len(raw_paths)} paths: {', '.join(raw_paths)}. "
            "Run max2-splitter before conversion or pass an explicit raw dataset path."
        )

    if DEFAULT_MAXWELL_RAW_DATASET_PATH in raw_paths:
        return DEFAULT_MAXWELL_RAW_DATASET_PATH
    return raw_paths[0]


def well_has_other_recordings(input_file: Path, raw_dataset_path: str) -> bool:
    """True when the raw dataset's well also lists other recordings (wells/<well>/recNNNN).

    Some MaxOne files hold one long recording plus short segments without routed raw data
    (production example: 2024-04-19-butterfly p001237/exp1/exp1_cartpole_long_21.raw.h5, a
    2.4 h rec0000 and thirteen 30 s rec0001-rec0013 with no groups/routed/raw). neo then refuses
    the file ("Detected multiple recording IDs across wells").
    """
    data_group_path = normalize_h5_path(raw_dataset_path)[: -len("/groups/routed/raw")]
    with h5py.File(input_file, "r") as file_handle:
        well_name = _well_of_data_group(file_handle, data_group_path)
        return well_name is not None and len(file_handle["wells"][well_name]) > 1


def _well_of_data_group(file_handle: h5py.File, data_group_path: str) -> str | None:
    if "wells" not in file_handle:
        return None
    target = file_handle[data_group_path].id
    for well_name, well in file_handle["wells"].items():
        if any(rec.id == target for rec in well.values()):
            return well_name
    return None


def create_maxone_compatibility_view(
    *,
    source_file: Path,
    raw_dataset_path: str,
    output_file: Path,
    force: bool = False,
    deduplicate_mapping: bool = False,
    single_recording: bool = False,
) -> Path:
    """Create a lightweight HDF5 file that exposes one source data group as data0000.

    The file uses HDF5 external links, so it does not duplicate the recording payload.
    It is used only as a NeuroConv compatibility input when split Max Two files keep
    their original data_store/dataNNNN path, or, with deduplicate_mapping, when the
    recording's settings/mapping lists a channel more than once (see
    deduplicated_channel_mapping); only that mapping is rewritten. With single_recording,
    the view's well lists only the recording that is this data group (see
    well_has_other_recordings).
    """

    raw_dataset_path = normalize_h5_path(raw_dataset_path)
    if raw_dataset_path == DEFAULT_MAXWELL_RAW_DATASET_PATH and not (force or deduplicate_mapping or single_recording):
        return source_file

    source_file = source_file.resolve()
    output_file.parent.mkdir(parents=True, exist_ok=True)
    if output_file.exists():
        output_file.unlink()

    data_group_path = raw_dataset_path[: -len("/groups/routed/raw")]
    if not data_group_path.startswith("/data_store/data"):
        raise ValueError(f"Unsupported Maxwell raw dataset path for compatibility view: {raw_dataset_path}")
    selected_well = infer_well_name_from_raw_dataset_path(raw_dataset_path)

    with h5py.File(source_file, "r") as source, h5py.File(output_file, "w") as destination:
        # A store number is independent of the physical well number. Resolve its
        # actual recording link for ordinary views as well as multi-recording views.
        selected_well = _well_of_data_group(source, data_group_path) or selected_well
        for key, value in source.attrs.items():
            destination.attrs[key] = value

        for key in source.keys():
            if key in {"data_store", "wells"}:
                continue
            destination[key] = h5py.ExternalLink(str(source_file), f"/{key}")

        data_store = destination.create_group("data_store")
        if "data_store" in source:
            for key, value in source["data_store"].attrs.items():
                data_store.attrs[key] = value
        if deduplicate_mapping:
            mapping, _ = deduplicated_channel_mapping(source, data_group_path)
            data_group = _linked_group_copy(source, destination, data_group_path, "/data_store/data0000", skip={"settings"})
            settings = _linked_group_copy(
                source, destination, f"{data_group_path}/settings", "/data_store/data0000/settings", skip={"mapping"}
            )
            settings.create_dataset("mapping", data=mapping)
        else:
            data_store["data0000"] = h5py.ExternalLink(str(source_file), data_group_path)
            data_group = None

        if selected_well and "wells" in source and selected_well in source["wells"]:
            wells = destination.create_group("wells")
            for key, value in source["wells"].attrs.items():
                wells.attrs[key] = value
            if deduplicate_mapping or single_recording:
                # Readers reach the recording through /wells/<well>/<rec>; point the recording
                # that is this data group at the rewritten copy (or the source group), and keep
                # the well's other recordings unless single_recording.
                well = wells.create_group(selected_well)
                for key, value in source["wells"][selected_well].attrs.items():
                    well.attrs[key] = value
                for rec_name, rec in source["wells"][selected_well].items():
                    if rec.id == source[data_group_path].id:
                        well[rec_name] = data_group if data_group is not None else h5py.ExternalLink(
                            str(source_file), data_group_path
                        )
                    elif not single_recording:
                        well[rec_name] = h5py.ExternalLink(str(source_file), f"/wells/{selected_well}/{rec_name}")
            else:
                wells[selected_well] = h5py.ExternalLink(str(source_file), f"/wells/{selected_well}")

    return output_file


def _linked_group_copy(
    source: h5py.File, destination: h5py.File, source_path: str, destination_path: str, *, skip: set[str]
) -> h5py.Group:
    """A real group with the source group's attributes whose members are external links, except `skip`."""
    group = destination.create_group(destination_path)
    for key, value in source[source_path].attrs.items():
        group.attrs[key] = value
    for key in source[source_path].keys():
        if key not in skip:
            group[key] = h5py.ExternalLink(source.filename, f"{source_path}/{key}")
    return group


def mapping_has_duplicate_channels(input_file: Path, raw_dataset_path: str) -> bool:
    """True when the recording's settings/mapping lists a routed channel more than once.

    Maxwell writes one mapping row per (channel, electrode) connection. When several electrodes
    are wired to one amplifier channel (e.g. grouped stimulation sites), that channel appears once
    per electrode, while groups/routed/raw still holds one row per channel. neo and probeinterface
    assume one mapping row per raw row and reject or misalign such files.
    """
    data_group_path = normalize_h5_path(raw_dataset_path)[: -len("/groups/routed/raw")]
    with h5py.File(input_file, "r") as file_handle:
        mapping_path = f"{data_group_path}/settings/mapping"
        if mapping_path not in file_handle:
            return False
        channels = np.asarray(file_handle[mapping_path]["channel"])
    channels = channels[channels >= 0]
    return len(np.unique(channels)) != len(channels)


def deduplicated_channel_mapping(file_handle: h5py.File, data_group_path: str) -> tuple[np.ndarray, dict[str, str]]:
    """One settings/mapping row per routed channel, in groups/routed/raw row order.

    In Maxwell stimulation configurations a block of k electrodes can be wired together onto k
    channels; the mapping then lists every channel of the block with every electrode of the block
    (2025-08-03-fife frequencystimphase files: 8 channels x 8 electrodes and 2 x 2), in no
    consistent order, and does not record which electrode a channel would read on its own. Each
    channel still needs one distinct electrode and position for the electrode table (probeinterface
    requires unique contact positions), so single-electrode channels keep theirs and each
    multi-electrode channel, in routed order, takes the first electrode it lists that no other
    channel has taken. The second value maps each channel id (the string neo uses) to every
    electrode wired to it, ';'-separated in mapping order.
    """
    mapping = file_handle[f"{data_group_path}/settings/mapping"][()]
    routed = np.asarray(file_handle[f"{data_group_path}/groups/routed/channels"][()])
    if routed.dtype.names:
        routed = np.asarray(routed["channel"])
    rows_by_channel = {}
    for channel in routed:
        matches = np.flatnonzero(mapping["channel"] == channel)
        if matches.size == 0:
            raise ValueError(f"Routed channel {int(channel)} has no settings/mapping row in {data_group_path}")
        rows_by_channel[int(channel)] = mapping[matches]
    chosen = {c: rows[0] for c, rows in rows_by_channel.items() if len(rows) == 1}
    taken = {int(row["electrode"]) for row in chosen.values()}
    for channel, rows in rows_by_channel.items():
        if len(rows) == 1:
            continue
        free = [row for row in rows if int(row["electrode"]) not in taken]
        if not free:
            raise ValueError(
                f"Channel {channel} in {data_group_path} is wired only to electrodes already assigned to other "
                f"channels ({';'.join(str(int(e)) for e in rows['electrode'])}); cannot give it a distinct electrode"
            )
        chosen[channel] = free[0]
        taken.add(int(free[0]["electrode"]))
    wired = {str(c): ";".join(str(int(e)) for e in rows["electrode"]) for c, rows in rows_by_channel.items()}
    return np.array([chosen[int(c)] for c in routed], dtype=mapping.dtype), wired
