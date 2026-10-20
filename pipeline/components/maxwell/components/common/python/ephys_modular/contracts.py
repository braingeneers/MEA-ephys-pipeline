"""Version-one artifact exchange. No service, science or storage operations here."""

import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from jsonschema import Draft7Validator

MAX_JSON_BYTES = 32 * 1024 * 1024
EPHYS = "braingeneers.ephys-artifacts/v1"
BRAINDANCE = "braingeneers.braindance-artifacts/v1"
_SCHEMA = json.loads((Path(__file__).parent / "schemas/artifacts-v1.schema.json").read_text())
Draft7Validator.check_schema(_SCHEMA)
_KINDS = set(_SCHEMA["definitions"]["kind"]["enum"])
_FAMILY_KINDS = {
    EPHYS: {"selection", "split", "conversion", "sorting", "curation", "overview", "unit_figures", "finalization"},
    BRAINDANCE: {"selection", "braindance_sort", "braindance_nwb", "braindance_finalization"},
}
_SKIPS = {
    "sorting": {"low_activity", "insufficient_duration", "legacy_sorter_limitation"},
    "braindance_sort": {"no_spontaneous_recording", "not_discoverable", "not_in_groups"},
}
_INVALIDATES = {
    "selection": _KINDS - {"selection"},
    "split": {"conversion", "sorting", "curation", "overview", "unit_figures", "finalization"},
    "conversion": {"sorting", "curation", "overview", "unit_figures", "finalization"},
    "sorting": {"curation", "overview", "unit_figures", "finalization"},
    "curation": {"overview", "unit_figures", "finalization"},
    "overview": {"finalization"},
    "unit_figures": {"finalization"},
    "braindance_sort": {"braindance_nwb", "braindance_finalization"},
    "braindance_nwb": {"braindance_finalization"},
}


class ContractError(ValueError):
    pass


def canonical_json(value):
    """Finite JSON only; preserve exact numeric encodings and undeclared keys."""
    def keys(item):
        if isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                raise ContractError("JSON object keys must be strings")
            for child in item.values():
                keys(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                keys(child)
    try:
        keys(value)
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ContractError("Expected finite, JSON-compatible metadata") from error
    if len(data) > MAX_JSON_BYTES:
        raise ContractError("Metadata exceeds the 32 MiB JSON limit; use an S3 inventory reference")
    return data


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def decode_json(data):
    if len(data) > MAX_JSON_BYTES:
        raise ContractError("Metadata exceeds the 32 MiB JSON limit")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError("Duplicate JSON object key: " + key)
            result[key] = value
        return result
    def invalid_constant(value):
        raise ContractError("JSON constants must be finite")
    try:
        result = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError) as error:
        raise ContractError("Invalid JSON metadata") from error
    canonical_json(result)
    return result


def parse_s3(uri, prefix=False):
    if not isinstance(uri, str) or any(ord(c) < 32 or ord(c) == 127 for c in uri):
        raise ContractError("S3 locations must be explicit URI strings without control characters")
    try:
        parsed = urlsplit(uri)
    except ValueError as error:
        raise ContractError("Malformed S3 URI") from error
    if (parsed.scheme != "s3" or not parsed.netloc or parsed.query or parsed.fragment
            or parsed.username or parsed.password or ":" in parsed.netloc):
        raise ContractError("Expected an explicit s3://bucket/key location")
    key = parsed.path[1:] if parsed.path.startswith("/") else parsed.path
    parts = key.rstrip("/").split("/")
    if not key or any(part in {"", ".", ".."} for part in parts):
        raise ContractError("S3 keys must be nonempty without relative or empty path components")
    if prefix and not key.endswith("/"):
        raise ContractError("Dataset/output prefixes must end in /")
    if not prefix and key.endswith("/"):
        raise ContractError("An object reference must identify a file, not a prefix")
    return parsed.netloc, key


def _transport(value):
    if type(value) is not str or value not in {"s3", "local"}:
        raise ContractError("Transport must be explicitly s3 or local")
    return value


def parse_file(uri, prefix=False):
    """Canonical absolute POSIX file URIs, for explicitly local execution only."""
    if not isinstance(uri, str) or any(ord(c) < 32 or ord(c) == 127 for c in uri):
        raise ContractError("Local locations must be explicit canonical file:// URIs")
    try:
        parsed = urlsplit(uri)
        decoded = unquote(parsed.path, encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError) as error:
        raise ContractError("Malformed local file URI") from error
    if (parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment
            or not decoded.startswith("/") or decoded == "/"
            or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
            or any(part in {".", "..", ""} for part in decoded.strip("/").split("/"))):
        raise ContractError("Expected an absolute file URI without host or relative components")
    path = PurePosixPath(decoded)
    canonical = path.as_uri() + ("/" if prefix else "")
    if uri != canonical:
        raise ContractError("Local file URI must use canonical escaping and prefix slash")
    return Path(str(path))


def validate_root(uri, transport="s3"):
    if _transport(transport) == "local":
        parse_file(uri, prefix=True)
    else:
        parse_s3(uri, prefix=True)
    return uri


def assert_within(uri, root, transport="s3"):
    if _transport(transport) == "local":
        target, base = parse_file(uri), parse_file(root, prefix=True)
        try:
            target.relative_to(base)
        except ValueError as error:
            raise ContractError("Mutation destination is outside the reviewed local prefix") from error
        if target == base:
            raise ContractError("Mutation destination must be a file beneath the local prefix")
        return
    bucket, key = parse_s3(uri)
    root_bucket, root_key = parse_s3(root, prefix=True)
    if bucket != root_bucket or not key.startswith(root_key):
        raise ContractError("Mutation destination is outside the reviewed dataset/output prefix")


def _shape(value, definition):
    error = next(Draft7Validator({"$ref": "#/definitions/" + definition,
                                 "definitions": _SCHEMA["definitions"]}).iter_errors(value), None)
    if error:
        location = ".".join(str(p) for p in error.absolute_path) or definition
        # Values/settings can be private; do not interpolate validation error.message.
        raise ContractError("Invalid " + definition + " at " + location + " (" + error.validator + ")")


def validate_ref(ref, strong=False, transport="s3"):
    _transport(transport)
    canonical_json(ref)
    _shape(ref, "manifest_ref" if strong else "object_ref")
    if type(ref["size"]) is not int:
        raise ContractError("Object sizes must be JSON integers")
    if transport == "local" and ref["uri"].startswith("file:"):
        parse_file(ref["uri"])
        if ("sha256" not in ref or ref["etag"] != "sha256-" + ref["sha256"]
                or "version_id" in ref):
            raise ContractError("Local references require an exact SHA-256 identity without versions")
    else:
        parse_s3(ref["uri"])
    return ref


def object_ref(uri, size, etag, sha256=None, version_id=None, transport="s3"):
    if isinstance(etag, str) and len(etag) >= 2 and etag[0] == etag[-1] == '"':
        etag = etag[1:-1]
    value = {"uri": uri, "size": size, "etag": etag}
    if sha256 is not None:
        value["sha256"] = sha256
    if version_id is not None:
        value["version_id"] = version_id
    return copy.deepcopy(validate_ref(value, transport=transport))


def _unique(values, label):
    if len(values) != len(set(values)):
        raise ContractError("Duplicate " + label)


def _version_one(value):
    # JSON Schema const treats 1.0 as 1; the wire format requires an integer.
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1):
        raise ContractError("Artifact schema_version must be the JSON integer 1")


def validate_envelope(value, dataset_uri=None, transport="s3"):
    _transport(transport)
    canonical_json(value)
    _version_one(value)
    _shape(value, "envelope")
    validate_ref(value["selection"], strong=True, transport=transport)
    if "parent" in value:
        validate_ref(value["parent"], strong=True, transport=transport)
    for stage in value["stages"]:
        validate_ref(stage["manifest"], strong=True, transport=transport)
    validate_root(value["dataset_uri"], transport)
    if dataset_uri is not None and value["dataset_uri"] != dataset_uri:
        raise ContractError("Artifact dataset does not match the reviewed output prefix")
    _unique([s["id"] for s in value["stages"]], "stage IDs")
    by_id = {s["id"]: s for s in value["stages"]}
    if any(s["kind"] not in _FAMILY_KINDS[value["contract"]] for s in value["stages"]):
        raise ContractError("Stage kind belongs to a different scientific contract family")
    for kind, stage_id in value["selected"].items():
        if kind not in _KINDS or stage_id not in by_id or by_id[stage_id]["kind"] != kind:
            raise ContractError("Selected stage is missing or has a different kind")
    return value


def new_envelope(contract, dataset_uri, selection, transport="s3"):
    value = {"schema_version": 1, "document_type": "envelope", "contract": contract,
             "dataset_uri": dataset_uri, "selection": copy.deepcopy(selection),
             "stages": [], "selected": {}}
    return validate_envelope(value, transport=transport)


def stage_signature(value):
    """New stage namespace only; never replaces the legacy collection signature."""
    fields = ["contract", "kind", "variant", "dataset_uri", "selection", "owner",
              "settings", "inputs", "unit_inputs"]
    return digest({key: value[key] for key in fields})


def validate_stage(value, expected_units=None, transport="s3"):
    _transport(transport)
    canonical_json(value)
    _version_one(value)
    _shape(value, "stage_result")
    validate_ref(value["selection"], strong=True, transport=transport)
    for item in value["inputs"]:
        validate_ref(item["manifest"], strong=True, transport=transport)
    for unit in value["unit_inputs"]:
        for ref in unit["sources"]:
            validate_ref(ref, transport=transport)
        for key in ("projection", "target"):
            if key in unit:
                validate_ref(unit[key], strong=True, transport=transport)
    validate_root(value["dataset_uri"], transport)
    if value["kind"] not in _FAMILY_KINDS[value["contract"]]:
        raise ContractError("Stage kind belongs to a different scientific contract family")
    _unique([s["id"] for s in value["inputs"]], "input stage IDs")
    _unique([u["unit_id"] for u in value["unit_inputs"]], "unit inputs")
    _unique([o["unit_id"] for o in value["outcomes"]], "unit outcomes")
    units = {u["unit_id"] for u in value["unit_inputs"]}
    if {o["unit_id"] for o in value["outcomes"]} != units:
        raise ContractError("Stage must account for every input unit exactly once")
    if expected_units is not None and units != set(expected_units):
        raise ContractError("Stage unit coverage differs from the expected selection")
    for outcome in value["outcomes"]:
        for artifact in outcome["artifacts"]:
            validate_ref(artifact["object"], transport=transport)
        if "evidence" in outcome:
            validate_ref(outcome["evidence"], strong=True, transport=transport)
        _unique([(a["kind"], a["object"]["uri"]) for a in outcome["artifacts"]], "unit artifacts")
        if outcome["status"] == "skipped" and outcome["reason"] not in _SKIPS.get(value["kind"], set()):
            raise ContractError("Unrecognized skipped outcome for this stage kind")
        if outcome["status"] == "no_data" and (
                value["kind"] not in {"curation", "overview", "unit_figures", "braindance_nwb"}
                or outcome["reason"] not in {"upstream_skipped", "no_units"}):
            raise ContractError("Unrecognized no-data outcome")
        if outcome["status"] == "complete" and "reason" in outcome:
            raise ContractError("Completed outcomes must not carry a skipped reason")
    if value["signature"] != stage_signature(value):
        raise ContractError("Stage scientific identity does not match its inputs/settings")
    if value["id"] != digest({k: v for k, v in value.items() if k != "id"}):
        raise ContractError("Stage result identity does not match its frozen content")
    return value


def make_stage(contract, kind, variant, dataset_uri, selection, owner, submitted,
               effective, execution, unit_inputs, outcomes, inputs=(), transport="s3"):
    value = copy.deepcopy({"schema_version": 1, "document_type": "stage_result",
                           "contract": contract, "kind": kind, "variant": variant,
                           "dataset_uri": dataset_uri, "selection": selection, "owner": owner,
                           "settings": {"submitted": submitted, "effective": effective},
                           "execution": execution, "inputs": list(inputs),
                           "unit_inputs": unit_inputs, "outcomes": outcomes})
    value["signature"] = stage_signature(value)
    value["id"] = digest(value)
    return validate_stage(value, transport=transport)


def stage_ref(stage, manifest, transport="s3"):
    validate_stage(stage, transport=transport)
    validate_ref(manifest, strong=True, transport=transport)
    return {"id": stage["id"], "kind": stage["kind"], "variant": stage["variant"],
            "manifest": copy.deepcopy(manifest)}


def append_stage(envelope, parent_ref, stage, manifest_ref, transport="s3"):
    validate_envelope(envelope, transport=transport)
    validate_ref(parent_ref, strong=True, transport=transport)
    validate_stage(stage, transport=transport)
    if (stage["dataset_uri"], stage["contract"], stage["selection"]) != (
            envelope["dataset_uri"], envelope["contract"], envelope["selection"]):
        raise ContractError("Stage belongs to a different dataset, contract or selection")
    existing = {s["id"]: s for s in envelope["stages"]}
    for upstream in stage["inputs"]:
        if existing.get(upstream["id"]) != upstream:
            raise ContractError("Stage input is missing or conflicts with upstream evidence")
    ref = stage_ref(stage, manifest_ref, transport=transport)
    if stage["id"] in existing and existing[stage["id"]] != ref:
        raise ContractError("Conflicting manifest for an existing frozen stage ID")
    result = copy.deepcopy(envelope)
    if stage["id"] not in existing:
        result["stages"].append(ref)
    result["parent"] = copy.deepcopy(parent_ref)
    for kind in _INVALIDATES.get(stage["kind"], set()):
        result["selected"].pop(kind, None)
    result["selected"][stage["kind"]] = stage["id"]
    return validate_envelope(result, transport=transport)


def selected_stage(envelope, kind, transport="s3"):
    validate_envelope(envelope, transport=transport)
    stage_id = envelope["selected"].get(kind)
    if stage_id is None:
        raise ContractError("No currently selected " + kind + " stage")
    return copy.deepcopy(next(s for s in envelope["stages"] if s["id"] == stage_id))


def require_artifacts(stage, kinds, expected_units=None, transport="s3"):
    validate_stage(stage, expected_units, transport=transport)
    result = {}
    for outcome in stage["outcomes"]:
        if outcome["status"] != "complete":
            raise ContractError("Consumer must explicitly handle an accepted skipped/no-data unit")
        present = {a["kind"] for a in outcome["artifacts"]}
        if not set(kinds).issubset(present):
            raise ContractError("Upstream stage lacks a required scientific artifact kind")
        result[outcome["unit_id"]] = copy.deepcopy(outcome["artifacts"])
    return result


def stage_output_uri(unit_output_uri, kind, signature, run_id, step_id, transport="s3"):
    """Append to an existing per-unit collection path; do not re-hash that path."""
    validate_root(unit_output_uri, transport)
    for value in (run_id, step_id):
        _shape(value, "identifier")
    _shape(kind, "kind")
    _shape(signature, "digest")
    if transport == 'local':
        run_id, step_id = quote(run_id, safe=''), quote(step_id, safe='')
    return (unit_output_uri + kind + "/" + signature
            + "/" + run_id + "/" + step_id + "/")


def manifest_uri(dataset_uri, run_id, step_id, name="artifacts.json", transport="s3"):
    validate_root(dataset_uri, transport)
    for value in (run_id, step_id, name):
        _shape(value, "identifier")
    if not name.endswith(".json"):
        raise ContractError("Stage evidence filenames must end in .json")
    if transport == 'local':
        run_id, step_id, name = (quote(value, safe='') for value in (run_id, step_id, name))
    return dataset_uri + "derived/manifests/modular-v1/" + run_id + "/" + step_id + "/" + name
