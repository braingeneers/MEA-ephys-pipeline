"""v1.0.0 biological graph writer shared by conversion and publication runtimes."""
from __future__ import annotations
import copy
import hashlib
import math
import re
import struct
from datetime import datetime
from typing import Any


def parse_iso8601(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _present(record, names):
    return {key: copy.deepcopy(record[key]) for key in names
            if key in record and record[key] not in (None, "", [], {})}


NWB_FLOAT32_FIELDS = {'temperature_c', 'recording_duration_s', 'start_time_s', 'end_time_s'}


def nwb_float32(value):
    return struct.unpack('f', struct.pack('f', float(value)))[0]


def _coerce_optional_float_fields(record, names, path):
    for key in names:
        value = record.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"NWB metadata {path}.{key} must be a finite JSON number")
        try:
            fits_float32 = math.isfinite(nwb_float32(value))
        except OverflowError:
            fits_float32 = False
        if not fits_float32:
            raise ValueError(f"NWB metadata {path}.{key} exceeds the finite float32 range")
        record[key] = float(value)


def _container_name(value, fallback):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or fallback).strip()).strip("-") or fallback


def storage_name(kind, identity):
    # Types share a subject's HDF5 child namespace. The digest also distinguishes
    # IDs that normalize to the same readable spelling without changing IDs.
    digest = hashlib.sha256(str(identity).encode()).hexdigest()[:16]
    return f"{kind}-{_container_name(identity, kind)}-{digest}"


def validate_projection_export(projection):
    if not projection.get("recorded_culture_id") and not (projection.get("subject") or {}).get("culture_id"):
        return
    for collection in ("cell_lines", "cultures"):
        records = projection.get(collection) or {}
        if not isinstance(records, dict):
            raise ValueError(f"NWB metadata {collection} must be an object")
        for key, owner in records.items():
            if not isinstance(owner, dict):
                raise ValueError(f"NWB metadata {collection}.{key} must be an object")
            if collection == "cell_lines" and owner.get("parent_cell_line_id") is not None and not isinstance(owner["parent_cell_line_id"], str):
                raise ValueError(f"NWB metadata {collection}.{key}.parent_cell_line_id must be one parent ID")
            variants = owner.get("genetic_variants", [])
            if not isinstance(variants, list):
                raise ValueError(f"NWB metadata {collection}.{key}.genetic_variants must be a list")
            for index, variant in enumerate(variants):
                origin = variant.get("origin") if isinstance(variant, dict) else None
                if not isinstance(origin, str) or not origin.strip():
                    raise ValueError(f"NWB metadata {collection}.{key}.genetic_variants.{index}.origin requires author input before v1.0.0 export; choose unknown explicitly when appropriate")


def _construct_applications(records: object) -> tuple[list[object], dict[str, object]]:
    from ndx_cell_culture import ConstructApplication

    values: list[object] = []
    by_id: dict[str, object] = {}
    allowed = [
        "source",
        "delivery_route",
        "delivery_mechanism",
        "vector_type",
        "payload",
        "promoter",
        "genomic_persistence_state",
        "application_time_relative_to_recording",
        "age_modified",
        "age_modified_reference",
        "expression_status",
        "validation_method",
        "notes",
    ]
    for index, record in enumerate(records if isinstance(records, list) else []):
        if not isinstance(record, dict):
            continue
        application_id = str(record.get("application_id") or f"application-{index + 1}")
        kwargs = _present(record, allowed)
        kwargs.setdefault("vector_type", "")
        kwargs.setdefault("payload", "")
        value = ConstructApplication(
            name=storage_name("application", application_id),
            application_id=application_id,
            **kwargs,
        )
        values.append(value)
        by_id[application_id] = value
    return values, by_id


def _genetic_variants(
    records: object, applications: dict[str, object], owners=None
) -> list[object]:
    from ndx_cell_culture import GeneticVariant

    values: list[object] = []
    allowed = [
        "target_coordinates",
        "edit_description",
        "delivery_method",
        "zygosity_or_edit_state",
        "validation_status",
        "validation_method",
        "notes",
    ]
    for index, record in enumerate(records if isinstance(records, list) else []):
        if not isinstance(record, dict):
            continue
        variant_id = str(record.get("variant_id") or f"variant-{index + 1}")
        kwargs = _present(record, allowed)
        kwargs["target_symbol"] = str(record.get("target_symbol") or "")
        kwargs["edit_type"] = str(record.get("edit_type") or "")
        kwargs["origin"] = record["origin"]
        if record.get("method") not in (None, ""):
            kwargs["method"] = record["method"]
        related_id = str(record.get("related_application_id") or "")
        if related_id:
            owner = record.get("related_application_owner")
            targets = applications
            if isinstance(owner, dict):
                targets = (owners or {}).get((owner.get("collection"), owner.get("id")), ({}, {}))[1]
            if related_id not in targets:
                raise ValueError(f"Genetic variant {variant_id} references an unknown construct application {related_id}")
            kwargs["related_application"] = targets[related_id]
        values.append(
            GeneticVariant(
                name=storage_name("variant", variant_id),
                variant_id=variant_id,
                **kwargs,
            )
        )
    return values


def apply_cell_culture_projection(nwbfile: object, projection: dict[str, Any]) -> None:
    """Materialize the same ndx-cell-culture contract as Maxwell conversion."""
    from ndx_cell_culture import (
        CellCulture,
        CellCultureSubject,
        CellLine,
        CultureProtocol,
        ExperimentContext,
        Pharmacology,
    )
    from pynwb.file import Subject

    validate_projection_export(projection)
    line_records = projection.get("cell_lines") or {}
    culture_records = projection.get("cultures") or {}
    subject_record = projection.get("subject")
    context_record = copy.deepcopy(projection.get("context") or {})
    if not isinstance(subject_record, dict):
        return
    if not nwbfile.session_id:
        from legacy_cell_culture import retained_legacy_projection
        preserved = retained_legacy_projection(nwbfile)
        session = ((preserved or {}).get("context") or {}).get("session_id")
        if session:
            nwbfile.fields["session_id"] = session
    if not isinstance(context_record, dict):
        raise ValueError("NWB metadata context must be an object")
    _coerce_optional_float_fields(
        context_record, ["temperature_c", "recording_duration_s"], "context"
    )

    application_owners = {
        (collection, key): _construct_applications(record.get("construct_applications"))
        for collection, rows in (("cell_lines", line_records), ("cultures", culture_records))
        for key, record in rows.items() if isinstance(record, dict)
    }

    line_objects: dict[str, object] = {}
    building_lines: set[str] = set()

    def build_line(line_id: str) -> object | None:
        if line_id in line_objects:
            return line_objects[line_id]
        record = line_records.get(line_id) if isinstance(line_records, dict) else None
        if not line_id:
            return None
        if not isinstance(record, dict) or line_id in building_lines:
            raise ValueError(f"Invalid or cyclic source cell line reference: {line_id}")
        building_lines.add(line_id)
        parent = build_line(str(record.get("parent_cell_line_id") or ""))
        applications, applications_by_id = application_owners[("cell_lines", line_id)]
        variants = _genetic_variants(
            record.get("genetic_variants"), applications_by_id, application_owners
        )
        kwargs = _present(
            record,
            [
                "cell_line_name",
                "cell_line_rrid",
                "clone_id",
                "clonal_status",
                "passage_number",
                "disease_or_diagnosis",
                "reference_genome",
                "notes",
            ],
        )
        kwargs.update(
            {
                "cell_line_id": str(record.get("cell_line_id") or line_id),
                "cell_line_type": str(record.get("cell_line_type") or ""),
                "sample_label": str(record.get("sample_label") or ""),
                "species": str(record.get("species") or ""),
                "cell_source_type": str(record.get("cell_source_type") or ""),
            }
        )
        if parent is not None:
            kwargs["parent_cell_line"] = parent
        if applications:
            kwargs["construct_applications"] = applications
        if variants:
            kwargs["genetic_variants"] = variants
        value = CellLine(name=storage_name("line", line_id), **kwargs)
        line_objects[line_id] = value
        building_lines.discard(line_id)
        return value

    for line_id in line_records:
        build_line(str(line_id))

    culture_objects: dict[str, object] = {}
    building_cultures: set[str] = set()

    def build_culture(culture_id: str) -> object | None:
        if culture_id in culture_objects:
            return culture_objects[culture_id]
        record = (
            culture_records.get(culture_id)
            if isinstance(culture_records, dict)
            else None
        )
        if not culture_id:
            return None
        if not isinstance(record, dict) or culture_id in building_cultures:
            raise ValueError(f"Invalid or cyclic cultured preparation reference: {culture_id}")
        building_cultures.add(culture_id)
        sources = [
            build_line(str(line_id)) for line_id in record.get("source_line_ids", [])
        ]
        parents = [
            build_culture(str(parent_id))
            for parent_id in record.get("parent_culture_ids", [])
        ]
        applications, applications_by_id = application_owners[("cultures", culture_id)]
        variants = _genetic_variants(
            record.get("genetic_variants"), applications_by_id, application_owners
        )
        protocol_record = record.get("culture_protocol")
        protocol = None
        if isinstance(protocol_record, dict) and any(protocol_record.values()):
            protocol_id = str(
                protocol_record.get("protocol_id") or f"{culture_id}-protocol"
            )
            protocol = CultureProtocol(
                name=storage_name("protocol", protocol_id),
                protocol_id=protocol_id,
                **_present(
                    protocol_record,
                    [
                        "protocol_name",
                        "protocol_uri",
                        "protocol_doi",
                        "protocol_version",
                        "patterning_summary",
                        "media_summary",
                        "notes",
                    ],
                ),
            )
        kwargs = _present(
            record,
            [
                "culture_subtype",
                "batch_label",
                "age",
                "age_reference",
                "disease_or_diagnosis",
                "reference_genome",
                "notes",
            ],
        )
        kwargs.update(
            {
                "culture_id": str(record.get("culture_id") or culture_id),
                "culture_type": str(record.get("culture_type") or ""),
                "sample_label": str(record.get("sample_label") or ""),
                "species": str(record.get("species") or ""),
            }
        )
        if any(sources):
            kwargs["source_lines"] = [value for value in sources if value is not None]
        if any(parents):
            kwargs["parent_cultures"] = [value for value in parents if value is not None]
        if applications:
            kwargs["construct_applications"] = applications
        if variants:
            kwargs["genetic_variants"] = variants
        if protocol is not None:
            kwargs["culture_protocol"] = protocol
        value = CellCulture(name=storage_name("culture", culture_id), **kwargs)
        culture_objects[culture_id] = value
        building_cultures.discard(culture_id)
        return value

    recorded_culture_id = str(
        projection.get("recorded_culture_id")
        or subject_record.get("culture_id")
        or ""
    )
    recorded_culture = build_culture(recorded_culture_id)
    for key in culture_records:
        build_culture(key)
    subject_kwargs = _present(
        subject_record,
        ["age", "age__reference", "description", "genotype", "sex", "species", "strain", "weight"],
    )
    if subject_record.get("date_of_birth"):
        subject_kwargs["date_of_birth"] = parse_iso8601(
            str(subject_record["date_of_birth"])
        )
    subject_id = str(subject_record.get("subject_id") or "")
    if recorded_culture is None:
        nwbfile.subject = Subject(subject_id=subject_id, **subject_kwargs)
        return

    subject = CellCultureSubject(
        subject_id=subject_id,
        culture=recorded_culture,
        cell_lines=list(line_objects.values()),
        related_cultures=[
            value
            for culture_id, value in culture_objects.items()
            if culture_id != recorded_culture_id
        ],
        **subject_kwargs,
    )
    nwbfile.subject = subject
    if context_record.get("_legacy_context_absent"):
        return
    context_kwargs = _present(
        context_record,
        [
            "age_reference",
            "media_or_bath",
            "experimental_setup",
            "notes",
            "temperature_c",
            "recording_duration_s",
            "spontaneous_activity",
            "electrical_stimulation",
            "optical_stimulation",
            "pharmacology_present",
        ],
    )
    devices = getattr(nwbfile, "devices", {})
    if "_legacy_device_name" in context_record:
        name = context_record["_legacy_device_name"]
        if name:
            if name not in devices:
                raise ValueError("Legacy recording device is missing from the NWB copy")
            context_kwargs["device"] = devices[name]
    elif devices:
        context_kwargs["device"] = next(iter(devices.values()))
    experiment_id = str(context_record.get("experiment_id") or "experiment-context")
    pharmacologies = []
    used_names = set()
    for index, source_record in enumerate(context_record.get("pharmacology", [])):
        if not isinstance(source_record, dict):
            continue
        record = copy.deepcopy(source_record)
        _coerce_optional_float_fields(
            record,
            ["start_time_s", "end_time_s"],
            f"context.pharmacology.{index}",
        )
        pharmacology_id = str(
            record.get("pharmacology_id") or f"pharmacology-{index + 1}"
        )
        name = _container_name(pharmacology_id, f"pharmacology-{index + 1}")
        if name in used_names:
            name = f"pharmacology-{name}"
        while name in used_names:
            name = f"{name}-{index + 1}"
        used_names.add(name)
        pharmacologies.append(
            Pharmacology(
                name=name,
                pharmacology_id=pharmacology_id,
                agent=str(record.get("agent") or ""),
                **_present(
                    record,
                    [
                        "concentration",
                        "concentration_unit",
                        "purpose",
                        "notes",
                        "start_time_s",
                        "end_time_s",
                    ],
                ),
            )
        )
    nwbfile.add_lab_meta_data(ExperimentContext(
        name="experiment_context",
        experiment_id=experiment_id,
        age_at_recording=str(context_record.get("age_at_recording") or ""),
        recording_platform=str(context_record.get("recording_platform") or "MEA"),
        pharmacologies=pharmacologies,
        **context_kwargs,
    ))



def verify_cell_culture_projection(nwbfile, projection):
    """Compare every supplied modeled biological field after a real NWB read."""
    from legacy_cell_culture import FIELDS, SUBJECT_FIELDS, plain
    differences = []
    subject = nwbfile.subject
    expected_subject = projection.get('subject')
    if not isinstance(expected_subject, dict):
        return differences
    def same(actual, expected):
        actual, expected = plain(actual), plain(expected)
        if expected in (None, ''):
            return actual in (None, '')
        if isinstance(expected, str) and isinstance(actual, datetime):
            return actual == parse_iso8601(expected)
        return actual == expected
    def fields(obj, record, names, path):
        for key in names:
            actual = getattr(obj, key, None)
            expected = nwb_float32(record[key]) if key in NWB_FLOAT32_FIELDS and record.get(key) not in (None, '') else record.get(key)
            if key in NWB_FLOAT32_FIELDS and actual not in (None, ''):
                actual = nwb_float32(actual)
            if key in record and not same(actual, expected):
                differences.append(path + '.' + key)
    fields(subject, expected_subject, SUBJECT_FIELDS + ('age__reference',), 'subject')
    recorded = projection.get('recorded_culture_id') or expected_subject.get('culture_id')
    if not recorded:
        return differences
    if getattr(getattr(subject, 'culture', None), 'name', None) != storage_name('culture', recorded):
        differences.append('subject.culture')
    def refs(value):
        if value is None:
            return []
        if hasattr(value, 'data'):
            return list(value.data)
        if isinstance(value, dict):
            return list(value.values())
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]
    for collection, kind, typename in (('cell_lines', 'line', 'CellLine'), ('cultures', 'culture', 'CellCulture')):
        actual = getattr(subject, 'cell_lines' if kind == 'line' else 'related_cultures', {})
        wanted = projection.get(collection) or {}
        if set(actual) != {storage_name(kind, key) for key in wanted}:
            differences.append(collection)
        for key, record in wanted.items():
            obj = actual.get(storage_name(kind, key))
            if obj is None:
                continue
            path = collection + '.' + key
            fields(obj, record, FIELDS[typename], path)
            for field, target_kind, stored_field in (('parent_cell_line_id', 'line', 'parent_cell_line'), ('source_line_ids', 'line', 'source_lines'), ('parent_culture_ids', 'culture', 'parent_cultures')):
                if (kind == 'line') != (field == 'parent_cell_line_id'):
                    continue
                expected = record.get(field) or []
                expected = [expected] if isinstance(expected, str) else expected
                if [value.name for value in refs(getattr(obj, stored_field, None))] != [storage_name(target_kind, value) for value in expected]:
                    differences.append(path + '.' + field)
            for field, typename_child, identity in (('construct_applications', 'ConstructApplication', 'application_id'), ('genetic_variants', 'GeneticVariant', 'variant_id')):
                expected = record.get(field) or []
                values = {getattr(value, identity): value for value in refs(getattr(obj, field, None))}
                if set(values) != {row.get(identity) for row in expected}:
                    differences.append(path + '.' + field)
                for row in expected:
                    value = values.get(row.get(identity))
                    if value is not None:
                        fields(value, row, FIELDS[typename_child], path + '.' + field + '.' + str(row.get(identity)))
                        if field == 'genetic_variants':
                            related = getattr(value, 'related_application', None)
                            owner = row.get('related_application_owner')
                            if isinstance(owner, dict) and getattr(getattr(related, 'parent', None), 'name', None) != storage_name('line' if owner['collection'] == 'cell_lines' else 'culture', owner['id']):
                                differences.append(path + '.related_application_owner')
                            if getattr(related, 'application_id', None) != (row.get('related_application_id') or None):
                                differences.append(path + '.related_application_id')
            if isinstance(record.get('culture_protocol'), dict):
                fields(getattr(obj, 'culture_protocol', None), record['culture_protocol'], FIELDS['CultureProtocol'], path + '.culture_protocol')
    context = nwbfile.lab_meta_data.get('experiment_context')
    if (projection.get('context') or {}).get('_legacy_context_absent'):
        return differences + (['context.unexpected'] if context is not None else [])
    if context is None:
        return differences + ['context']
    if '_legacy_device_name' in (projection.get('context') or {}) and getattr(getattr(context, 'device', None), 'name', '') != projection['context']['_legacy_device_name']:
        differences.append('context.device')
    fields(context, projection.get('context') or {}, tuple(key for key in FIELDS['ExperimentContext'] if key != 'session_id'), 'context')
    expected = (projection.get('context') or {}).get('pharmacology') or []
    values = {row.pharmacology_id: row for row in context.pharmacologies.values()}
    if set(values) != {row.get('pharmacology_id') for row in expected}:
        differences.append('context.pharmacology')
    for row in expected:
        obj = values.get(row.get('pharmacology_id'))
        if obj is not None:
            fields(obj, row, FIELDS['Pharmacology'], 'context.pharmacology.' + str(row.get('pharmacology_id')))
            if obj.experiment is not context:
                differences.append('context.pharmacology.experiment')
    return differences
