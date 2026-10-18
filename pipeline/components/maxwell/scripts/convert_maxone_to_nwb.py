#!/usr/bin/env python3
"""Convert a Maxwell Max One recording to NWB using NeuroConv."""

from __future__ import annotations

from workflow_defaults import scientific_default

import argparse
import importlib
import inspect
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from neuroconv.tools.nwb_helpers import configure_and_write_nwbfile
from neuroconv.tools.nwb_helpers._configuration_models._hdf5_dataset_io import (
    HDF5DatasetIOConfiguration,
)
from h5py._hl.filters import FilterRefBase

from maxwell_h5_utils import (
    DEFAULT_MAXWELL_RAW_DATASET_PATH,
    configure_bundled_maxwell_hdf5_plugin,
    count_routed_raw_dataset_paths,
    create_maxone_compatibility_view,
    deduplicated_channel_mapping,
    discover_routed_raw_dataset_path,
    mapping_has_duplicate_channels,
    well_has_other_recordings,
    require_hdf5_filter_capabilities,
)

import h5py

from cell_culture_projection import apply_cell_culture_projection, validate_projection_export


def fail(message: str) -> None:
    print(f"[convert_maxone_to_nwb] {message}", file=sys.stderr)
    raise SystemExit(1)


def load_maxone_interface_class():
    candidates = [
        ("neuroconv.datainterfaces", "MaxOneRecordingInterface"),
        ("neuroconv.datainterfaces.ecephys", "MaxOneRecordingInterface"),
    ]

    for module_name, class_name in candidates:
        try:
            module = importlib.import_module(module_name)
            return getattr(module, class_name)
        except Exception:
            continue

    fail(
        "Could not import MaxOneRecordingInterface from NeuroConv. "
        "Verify the container includes NeuroConv with Maxwell support."
    )


def parse_iso8601(timestamp: str | None) -> datetime:
    if not timestamp:
        return datetime.now(timezone.utc)

    normalized = timestamp.replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        fail(f"Invalid --session-start-time '{timestamp}': {exc}")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", required=True, help="Path to Maxwell .raw.h5 file")
    parser.add_argument("--output-file", required=True, help="Destination NWB path")
    parser.add_argument(
        "--session-description",
        default=scientific_default('session_description'),
        help="NWB session description",
    )
    parser.add_argument(
        "--session-identifier-prefix",
        default=scientific_default('session_identifier_prefix'),
        help="Prefix used to build NWB identifier",
    )
    parser.add_argument(
        "--session-start-time",
        default=scientific_default('session_start_time'),
        help="ISO-8601 time, e.g. 2026-03-04T12:34:56Z. Defaults to conversion time.",
    )
    parser.add_argument("--subject-id", default=scientific_default('subject_id'), help="Optional NWB Subject.subject_id")
    parser.add_argument("--subject-species", default=scientific_default('subject_species'), help="Optional NWB Subject.species")
    parser.add_argument(
        "--subject-description",
        default=scientific_default('subject_description'),
        help="Optional NWB Subject.description",
    )
    parser.add_argument(
        "--nwb-metadata-file",
        default="",
        help="Optional verified uploader NWB metadata projection JSON.",
    )
    parser.add_argument(
        "--nwb-chunk-duration-ms",
        type=float,
        default=float(scientific_default('nwb_chunk_duration_ms')),
        help="Chunk duration in milliseconds for NWB ElectricalSeries/data",
    )
    parser.add_argument(
        "--nwb-chunk-channels",
        type=int,
        default=scientific_default('nwb_chunk_channels'),
        help=(
            "Channel count per chunk for NWB ElectricalSeries/data. "
            "Use 0 to chunk across all channels."
        ),
    )
    parser.add_argument(
        "--nwb-buffer-chunks",
        type=int,
        default=scientific_default('nwb_buffer_chunks'),
        help="Buffered chunk count used by DataChunkIterator writes",
    )
    parser.add_argument(
        "--maxwell-raw-dataset-path",
        default=scientific_default('maxwell_raw_dataset_path'),
        help="Raw dataset path in the Maxwell input, or 'auto' to discover it.",
    )
    return parser.parse_args()


def load_metadata_projection(path: str) -> dict[str, object] | None:
    if not path:
        return None
    metadata_path = Path(path)
    if not metadata_path.is_file():
        fail(f"NWB metadata projection does not exist: {metadata_path}")
    try:
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"Could not read NWB metadata projection: {exc}")
    if value.get("projection_schema_version") != 1:
        fail("Unsupported NWB metadata projection schema version.")
    return value


def _present(record: dict[str, object], names: list[str]) -> dict[str, object]:
    return {
        name: record[name]
        for name in names
        if name in record and record[name] not in (None, "", [], {})
    }


def _coerce_optional_float_fields(
    record: dict[str, object],
    names: list[str],
    path: str,
) -> None:
    for name in names:
        value = record.get(name)
        if value in (None, ""):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            fail(f"NWB metadata {path}.{name} must be a finite JSON number.")
        normalized = float(value)
        if not math.isfinite(normalized):
            fail(f"NWB metadata {path}.{name} must be a finite JSON number.")
        record[name] = normalized


def _container_name(value: object, fallback: str) -> str:
    text = str(value or fallback).strip()
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-")
    return text or fallback


def find_electrical_series_data_key(dataset_configurations: dict[str, object]) -> str:
    for key, config in dataset_configurations.items():
        location = getattr(config, "location_in_file", "")
        dataset_name = getattr(config, "dataset_name", "")
        if dataset_name == "data" and (
            str(location).endswith("ElectricalSeries/data") or str(key).endswith("ElectricalSeries/data")
        ):
            return key
    fail("Could not locate ElectricalSeries/data in backend configuration.")


class MaxwellMxwDataFilter(FilterRefBase):
    """HDF5 filter reference for Maxwell's custom mxw-data codec (filter id 401)."""

    filter_id = 401
    filter_options = ()


def maxwell_read_failure_message(
    exc: BaseException,
    input_file: Path,
    raw_dataset_path: str,
) -> str | None:
    """Explain a native Maxwell decompression failure without blaming a present plugin."""
    if "filter returned failure during read" not in str(exc).lower():
        return None
    return (
        f"Could not decode Maxwell raw data from {input_file} at {raw_dataset_path}: {exc}. "
        "HDF5 filter 401 is registered with decode support, so the compressed source data is unreadable "
        "or corrupt. Validate an independent acquisition copy before replacing or rerunning this recording."
    )


def main() -> None:
    args = parse_args()
    metadata_projection = load_metadata_projection(args.nwb_metadata_file)

    input_file = Path(args.input_file)
    output_file = Path(args.output_file)

    if not input_file.exists():
        fail(f"Input file does not exist: {input_file}")

    if not input_file.name.endswith(".raw.h5"):
        fail("Input must end with '.raw.h5' for Maxwell Max One data.")

    if args.nwb_chunk_duration_ms <= 0:
        fail("Parameter --nwb-chunk-duration-ms must be > 0.")

    if args.nwb_chunk_channels < 0:
        fail("Parameter --nwb-chunk-channels must be >= 0.")

    if args.nwb_buffer_chunks <= 0:
        fail("Parameter --nwb-buffer-chunks must be > 0.")

    try:
        raw_dataset_path = discover_routed_raw_dataset_path(
            input_file,
            requested_path=args.maxwell_raw_dataset_path,
            require_single=args.maxwell_raw_dataset_path.lower() == "auto",
        )
    except Exception as exc:
        fail(str(exc))

    try:
        plugin_path = str(configure_bundled_maxwell_hdf5_plugin())
        require_hdf5_filter_capabilities(401, require_decode=True, require_encode=True)
    except RuntimeError as exc:
        fail(str(exc))

    explicit_dataset = args.maxwell_raw_dataset_path.strip().lower() != "auto"
    force_compatibility_view = explicit_dataset and count_routed_raw_dataset_paths(input_file) > 1
    shared_channels = mapping_has_duplicate_channels(input_file, raw_dataset_path)
    other_recordings = well_has_other_recordings(input_file, raw_dataset_path)
    if other_recordings:
        print(
            "[convert_maxone_to_nwb] the well lists other recordings besides this raw dataset; "
            "converting only the recording that is this raw dataset"
        )
    interface_input_file = input_file
    if (
        raw_dataset_path != DEFAULT_MAXWELL_RAW_DATASET_PATH
        or force_compatibility_view
        or shared_channels
        or other_recordings
    ):
        try:
            interface_input_file = create_maxone_compatibility_view(
                source_file=input_file,
                raw_dataset_path=raw_dataset_path,
                output_file=output_file.parent / "maxwell_data0000_compat.raw.h5",
                force=force_compatibility_view,
                deduplicate_mapping=shared_channels,
                single_recording=other_recordings,
            )
        except Exception as exc:
            fail(f"Could not create MaxOne compatibility view: {exc}")

    interface_class = load_maxone_interface_class()
    signature = inspect.signature(interface_class)

    init_kwargs: dict[str, object] = {}
    if "file_path" in signature.parameters:
        init_kwargs["file_path"] = str(interface_input_file)
    else:
        fail("Unexpected NeuroConv MaxOneRecordingInterface signature: missing 'file_path'.")

    if "hdf5_plugin_path" in signature.parameters:
        init_kwargs["hdf5_plugin_path"] = plugin_path

    if "download_plugin" in signature.parameters:
        init_kwargs["download_plugin"] = False

    if "verbose" in signature.parameters:
        init_kwargs["verbose"] = True

    interface = interface_class(**init_kwargs)

    metadata = interface.get_metadata()
    if shared_channels:
        # Channels wired to several electrodes keep one electrode-table row each, with a distinct
        # electrode from their wired set (see deduplicated_channel_mapping); this column lists them all.
        data_group_path = raw_dataset_path[: -len("/groups/routed/raw")]
        with h5py.File(input_file, "r") as file_handle:
            _, wired = deduplicated_channel_mapping(file_handle, data_group_path)
        recording = interface.recording_extractor
        recording.set_property("wired_electrodes", [wired[str(c)] for c in recording.get_channel_ids()])
        metadata.setdefault("Ecephys", {}).setdefault("Electrodes", []).append({
            "name": "wired_electrodes",
            "description": (
                "Every electrode wired to this amplifier channel, ';'-separated in Maxwell settings/mapping "
                "order. Several entries mean the channel recorded those electrodes wired together (e.g. a "
                "stimulation block); the file does not record which one the channel would read alone, so the "
                "row's electrode id and location are a distinct electrode from this set: the first listed one "
                "not already assigned to another channel."
            ),
        })
        print(
            "[convert_maxone_to_nwb] settings/mapping lists some channels more than once; "
            f"{sum(';' in v for v in wired.values())} channels are wired to several electrodes"
        )
    nwb_file_metadata = metadata.setdefault("NWBFile", {})
    projection_context = (
        metadata_projection.get("context", {})
        if isinstance(metadata_projection, dict)
        else {}
    )
    projection_common = (
        metadata_projection.get("common", {})
        if isinstance(metadata_projection, dict)
        else {}
    )
    nwb_file_metadata["session_description"] = str(
        projection_context.get("session_description")
        or projection_common.get("experiment_description")
        or args.session_description
    )

    session_start_time = parse_iso8601(
        str(projection_context.get("session_start_time") or args.session_start_time)
    )
    nwb_file_metadata["session_start_time"] = session_start_time

    if metadata_projection:
        identifier_parts = [
            metadata_projection.get("dataset_uuid"),
            metadata_projection.get("experiment_name"),
            metadata_projection.get("unit"),
        ]
        nwb_file_metadata["identifier"] = ":".join(
            str(value).strip() for value in identifier_parts if str(value or "").strip()
        )
        nwb_file_metadata["session_id"] = str(
            projection_context.get("session_id")
            or f"{metadata_projection.get('experiment_name')}:{metadata_projection.get('unit')}"
        )
        people = metadata_projection.get("people") or {}
        experimenter_names = [
            str(person.get("name"))
            for person in people.values()
            if isinstance(person, dict) and person.get("name")
        ]
        if experimenter_names:
            nwb_file_metadata["experimenter"] = experimenter_names
        for field in (
            "lab",
            "institution",
            "experiment_description",
            "keywords",
            "related_publications",
            "notes",
            "protocol",
        ):
            value = projection_common.get(field)
            if value not in (None, "", [], {}):
                nwb_file_metadata[field] = value
    else:
        identifier_suffix = session_start_time.strftime("%Y%m%dT%H%M%SZ")
        nwb_file_metadata["identifier"] = f"{args.session_identifier_prefix}-{identifier_suffix}"

    subject_updates: dict[str, str] = {}
    if args.subject_id:
        subject_updates["subject_id"] = args.subject_id
    if args.subject_species:
        subject_updates["species"] = args.subject_species
    if args.subject_description:
        subject_updates["description"] = args.subject_description

    if metadata_projection:
        metadata.pop("Subject", None)
    elif subject_updates:
        subject_metadata = metadata.get("Subject", {})
        subject_metadata.update(subject_updates)
        metadata["Subject"] = subject_metadata
    elif "Subject" in metadata and not metadata["Subject"]:
        # NeuroConv/PynWB validation can fail when an empty Subject block is present.
        metadata.pop("Subject")

    output_file.parent.mkdir(parents=True, exist_ok=True)

    nwbfile = interface.create_nwbfile(metadata=metadata)
    if metadata_projection:
        apply_cell_culture_projection(nwbfile, metadata_projection)
    backend_configuration = interface.get_default_backend_configuration(nwbfile=nwbfile, backend="hdf5")

    target_key = find_electrical_series_data_key(backend_configuration.dataset_configurations)
    target_dataset_config = backend_configuration.dataset_configurations[target_key]

    full_shape = tuple(int(x) for x in target_dataset_config.full_shape)
    if len(full_shape) != 2:
        fail(f"Expected 2D ElectricalSeries/data, found shape {full_shape}.")
    n_timepoints, n_channels = full_shape

    electrical_series = nwbfile.acquisition.get("ElectricalSeries")
    sampling_rate = getattr(electrical_series, "rate", None)
    if sampling_rate is None:
        fail("Could not determine sampling rate from NWB ElectricalSeries.")
    sampling_rate = float(sampling_rate)

    samples_per_chunk = max(1, int(round(sampling_rate * args.nwb_chunk_duration_ms / 1000.0)))
    samples_per_chunk = min(samples_per_chunk, n_timepoints)
    if args.nwb_chunk_channels == 0:
        channels_per_chunk = n_channels
    else:
        channels_per_chunk = int(args.nwb_chunk_channels)
        if channels_per_chunk > n_channels:
            fail(
                "Parameter --nwb-chunk-channels exceeds available channels: "
                f"requested={channels_per_chunk}, available={n_channels}."
            )

    buffered_samples = min(n_timepoints, samples_per_chunk * args.nwb_buffer_chunks)
    buffered_samples = buffered_samples - (buffered_samples % samples_per_chunk)
    if buffered_samples <= 0:
        buffered_samples = samples_per_chunk

    compression_method = MaxwellMxwDataFilter()
    compression_options: dict[str, object] | None = None

    replacement_dataset_config = HDF5DatasetIOConfiguration(
        object_id=target_dataset_config.object_id,
        location_in_file=target_dataset_config.location_in_file,
        dataset_name=target_dataset_config.dataset_name,
        dtype=target_dataset_config.dtype,
        full_shape=target_dataset_config.full_shape,
        chunk_shape=(samples_per_chunk, channels_per_chunk),
        buffer_shape=(buffered_samples, n_channels),
        compression_method=compression_method,
        compression_options=compression_options,
    )
    backend_configuration.dataset_configurations[target_key] = replacement_dataset_config

    try:
        configure_and_write_nwbfile(
            nwbfile=nwbfile,
            nwbfile_path=str(output_file),
            backend_configuration=backend_configuration,
        )
    except OSError as exc:
        message = maxwell_read_failure_message(exc, input_file, raw_dataset_path)
        if message is None:
            raise
        raise RuntimeError(message) from exc

    if metadata_projection:
        from pynwb import NWBHDF5IO
        from ndx_cell_culture import validate_recommended_terms

        with NWBHDF5IO(str(output_file), "r", load_namespaces=True) as io:
            readback = io.read()
            recommended_issues = validate_recommended_terms(readback)
        for issue in recommended_issues:
            print(
                "[convert_maxone_to_nwb] Metadata recommendation: "
                f"{getattr(issue, 'message', issue)}"
            )
        for warning in metadata_projection.get("warnings", []):
            print(
                "[convert_maxone_to_nwb] DANDI completeness warning: "
                f"{warning.get('message', warning)}"
            )

    print(
        "[convert_maxone_to_nwb] Wrote NWB file: "
        f"{output_file} (shape={full_shape}, chunk={replacement_dataset_config.chunk_shape}, "
        f"compression={replacement_dataset_config.compression_method}, "
        f"compression_options={replacement_dataset_config.compression_options}, "
        f"raw_dataset_path={raw_dataset_path})"
    )


if __name__ == "__main__":
    main()
