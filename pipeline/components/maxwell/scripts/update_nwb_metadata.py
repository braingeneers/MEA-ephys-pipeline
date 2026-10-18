#!/usr/bin/env python3
"""Re-apply uploader-authored metadata to an existing NWB file in place.

When metadata is edited after conversion, this replaces the subject, the
cell-culture lineage, the culture experiment context, and the editable NWBFile
text fields from a new uploader NWB metadata projection. The identifier,
session_id, timestamps, devices, electrodes, and every acquisition and
processing dataset are left untouched, so neither the raw signal nor derived
products need to be rebuilt. The same projection application used during
conversion is reused, so an edited file matches what a fresh conversion would
have written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import h5py
from hdmf.build import BuildManager
from pynwb import NWBHDF5IO, get_type_map

from convert_maxone_to_nwb import (
    apply_cell_culture_projection,
    fail,
    load_metadata_projection,
)

REPLACED_GENERAL_GROUPS = ("subject", "experiment_context")

from cell_culture_projection import validate_projection_export, verify_cell_culture_projection
from legacy_cell_culture import (cell_culture_layout, metadata_graph_paths,
    migrate_legacy_copy, reject_external_metadata_references, retained_projection)
GENERAL_TEXT_FIELDS = ("lab", "institution", "experiment_description", "notes", "protocol")
GENERAL_LIST_FIELDS = ("keywords", "related_publications")
REVISION_SCRATCH_NAME = "nwb_metadata_projection_revision"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nwb-file", required=True, help="Local NWB file, modified in place.")
    parser.add_argument(
        "--nwb-metadata-file",
        required=True,
        help="Uploader NWB metadata projection JSON (projection_schema_version 1).",
    )
    parser.add_argument(
        "--nwb-metadata-sha256",
        required=True,
        help="Expected lowercase SHA-256 of the projection file.",
    )
    parser.add_argument("--report-file", default="nwb-metadata-update.json")
    return parser.parse_args()


def write_text(group: h5py.Group, name: str, value: object) -> None:
    if name in group:
        del group[name]
    if isinstance(value, (list, tuple)):
        group.create_dataset(name, data=[str(item) for item in value], dtype=h5py.string_dtype())
    else:
        group.create_dataset(name, data=str(value), dtype=h5py.string_dtype())


def replace_text_fields(nwb: h5py.File, projection: dict[str, object]) -> list[str]:
    """Mirror the converter's projection-driven NWBFile fields."""

    common = projection.get("common") or {}
    context = projection.get("context") or {}
    general = nwb.require_group("general")
    replaced: list[str] = []
    session_description = context.get("session_description") or common.get("experiment_description")
    if session_description:
        write_text(nwb, "session_description", session_description)
        replaced.append("session_description")
    people = projection.get("people") or {}
    experimenters = [
        str(person["name"])
        for person in people.values()
        if isinstance(person, dict) and person.get("name")
    ]
    if experimenters:
        write_text(general, "experimenter", experimenters)
        replaced.append("general/experimenter")
    for field in GENERAL_TEXT_FIELDS + GENERAL_LIST_FIELDS:
        value = common.get(field)
        if value in (None, "", [], {}):
            continue
        if field in GENERAL_LIST_FIELDS and not isinstance(value, (list, tuple)):
            value = [value]
        write_text(general, field, value)
        replaced.append(f"general/{field}")
    return replaced


def extension_aware_manager(nwb_path: Path) -> BuildManager:
    """Combine every registered extension with the file's cached namespaces.

    Opening an existing file with only its cached namespaces would silently
    write ndx-cell-culture objects as their nearest core base type and omit the
    extension specification, because files converted without a projection
    cache only core namespaces.
    """

    import ndx_cell_culture  # noqa: F401  (registers the namespace globally)

    type_map = get_type_map()
    NWBHDF5IO.load_namespaces(type_map.namespace_catalog, str(nwb_path))
    return BuildManager(type_map)


def main() -> None:
    args = parse_args()
    nwb_path = Path(args.nwb_file)
    projection_path = Path(args.nwb_metadata_file)
    expected_sha256 = args.nwb_metadata_sha256.strip()
    actual_sha256 = hashlib.sha256(projection_path.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        fail(f"NWB metadata projection SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}")
    projection = load_metadata_projection(str(projection_path))
    subject = projection.get("subject") if projection else None
    if not isinstance(subject, dict) or not str(subject.get("subject_id") or "").strip():
        fail("NWB metadata projection must contain a subject with subject_id; refusing to remove the existing subject.")

    validate_projection_export(projection)
    # Work on a disposable copy even for ordinary refreshes. Failure never
    # leaves a partially stripped subject/context in the staged input.
    with tempfile.TemporaryDirectory(prefix=".nwb-refresh-", dir=nwb_path.parent) as directory:
        staged = Path(directory) / "updated.nwb"
        migrated = cell_culture_layout(nwb_path)['layout'] == 'legacy'
        if migrated:
            migrate_legacy_copy(nwb_path, staged, projection)
        else:
            shutil.copyfile(nwb_path, staged)
        with NWBHDF5IO(str(staged), "r", manager=extension_aware_manager(staged)) as io:
            wanted = retained_projection(io.read(), projection)
        with h5py.File(staged, "a") as nwb:
            if "identifier" not in nwb or "general" not in nwb:
                fail(f"Not an NWB file: {nwb_path}")
            identifier = nwb["identifier"][()]
            general = nwb["general"]
            replaced_roots = []
            for name in list(general):
                obj = general[name]
                if name == "subject" or (isinstance(obj, h5py.Group) and obj.attrs.get("namespace") == "ndx-cell-culture" and obj.attrs.get("neurodata_type") == "ExperimentContext"):
                    replaced_roots.append('/general/' + name)
            reject_external_metadata_references(nwb,replaced_roots)
            for path in metadata_graph_paths(nwb,replaced_roots):
                del nwb[path]
            if "scratch" in nwb and REVISION_SCRATCH_NAME in nwb["scratch"]:
                del nwb["scratch"][REVISION_SCRATCH_NAME]
            replaced = replace_text_fields(nwb, wanted)
        revision = {"projection_sha256": actual_sha256, "dataset_uuid": projection.get("dataset_uuid"),
                    "replaced": list(REPLACED_GENERAL_GROUPS) + replaced, "legacy_migrated": migrated}
        with NWBHDF5IO(str(staged), "a", manager=extension_aware_manager(staged)) as io:
            nwbfile = io.read()
            apply_cell_culture_projection(nwbfile, wanted)
            nwbfile.add_scratch(json.dumps(revision, sort_keys=True), name=REVISION_SCRATCH_NAME,
                                description="Uploader NWB metadata projection most recently applied")
            io.write(nwbfile, cache_spec=True)
        with h5py.File(staged, "r") as nwb:
            if nwb["identifier"][()] != identifier:
                fail("NWB identifier changed during metadata update.")
        with NWBHDF5IO(str(staged), "r", load_namespaces=True) as io:
            readback = io.read()
            differences = verify_cell_culture_projection(readback, wanted)
            applied = readback.subject
            if differences:
                fail("Updated metadata failed readback verification: " + ", ".join(differences))
        staged.replace(nwb_path)

    report = {
        "nwb_file": str(nwb_path),
        "nwb_size_bytes": nwb_path.stat().st_size,
        "subject_id": applied.subject_id,
        **revision,
    }
    Path(args.report_file).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
