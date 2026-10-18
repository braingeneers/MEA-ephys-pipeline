"""Publish verified workflow-owned evidence before emitting the next reference."""

from .contracts import (ContractError, append_stage, assert_within, canonical_json, digest,
                        validate_envelope, validate_ref, validate_stage)


def load_envelope(store, reference, dataset_uri=None):
    transport = getattr(store, "transport", "s3")
    if isinstance(reference, str):
        value, ref = store.read_json(reference)
    else:
        validate_ref(reference, strong=True, transport=transport)
        value, ref = store.read_json(reference["uri"], expected=reference)
    validate_envelope(value, dataset_uri, transport=transport)
    return value, ref


def load_stage(store, reference, contract=None, dataset_uri=None, selection=None):
    transport = getattr(store, "transport", "s3")
    if selection is not None:
        validate_ref(selection, strong=True, transport=transport)
    ref = reference["manifest"]
    validate_ref(ref, strong=True, transport=transport)
    value, _ = store.read_json(ref["uri"], expected=ref)
    validate_stage(value, transport=transport)
    if any(value[key] != reference[key] for key in ("id", "kind", "variant")):
        raise ContractError("Referenced stage identity/kind/variant differs from its frozen result")
    if contract is not None and value["contract"] != contract:
        raise ContractError("Referenced stage has a different semantic contract")
    if dataset_uri is not None and value["dataset_uri"] != dataset_uri:
        raise ContractError("Referenced stage belongs to a different dataset")
    if selection is not None and value["selection"] != selection:
        raise ContractError("Referenced stage belongs to a different recording selection")
    return value


def publish_initial(store, envelope, uri):
    validate_envelope(envelope, store.output_root, transport=getattr(store, "transport", "s3"))
    if store.output_root is None:
        raise ContractError("Publication requires a reviewed dataset/output prefix")
    if envelope["stages"] or envelope["selected"] or "parent" in envelope:
        raise ContractError("Initial publication must not claim completed stages")
    store.read_json(envelope["selection"]["uri"], expected=envelope["selection"])
    return store.put_json(uri, envelope)


def publish_stage(store, envelope, parent_ref, stage, result_uri, envelope_uri,
                  expected_units=None):
    """No caller can emit the returned envelope URI before all these checks pass."""
    if store.output_root is None:
        raise ContractError("Publication requires a reviewed dataset/output prefix")
    transport = getattr(store, "transport", "s3")
    validate_envelope(envelope, store.output_root, transport=transport)
    validate_stage(stage, expected_units, transport=transport)
    assert_within(result_uri, store.output_root, transport=transport)
    assert_within(envelope_uri, store.output_root, transport=transport)
    validate_ref(parent_ref, strong=True, transport=transport)
    if (parent_ref["sha256"] != digest(envelope)
            or parent_ref["size"] != len(canonical_json(envelope))):
        raise ContractError("Parent reference does not bind this canonical envelope")
    # Validate graph/bindings before performing any mutation. The temporary result
    # reference is replaced below only after the real result is published.
    result_bytes = canonical_json(stage)
    placeholder = {"uri": result_uri, "size": len(result_bytes),
                   "etag": "sha256-" + digest(stage) if transport == "local" else "pending",
                   "sha256": digest(stage)}
    known = next((s for s in envelope["stages"] if s["id"] == stage["id"]), None)
    if known and known["manifest"]["uri"] != result_uri:
        raise ContractError("Existing stage ID has a different result destination")
    append_stage(envelope, parent_ref, stage, known["manifest"] if known else placeholder,
                 transport=transport)
    load_envelope(store, parent_ref, store.output_root)
    store.read_json(stage["selection"]["uri"], expected=stage["selection"])
    for upstream in stage["inputs"]:
        load_stage(store, upstream, stage["contract"], stage["dataset_uri"], stage["selection"])
    for unit in stage["unit_inputs"]:
        for source in unit["sources"]:
            store.verify(source)
        for key in ("projection", "target"):
            if key in unit:
                store.read_json(unit[key]["uri"], expected=unit[key])
    for outcome in stage["outcomes"]:
        for artifact in outcome["artifacts"]:
            store.verify(artifact["object"])
        if "evidence" in outcome:
            store.read_json(outcome["evidence"]["uri"], expected=outcome["evidence"])
    if known:
        load_stage(store, known, stage["contract"], stage["dataset_uri"], stage["selection"])
        result_ref = known["manifest"]
    else:
        result_ref = store.put_json(result_uri, stage)
    updated = append_stage(envelope, parent_ref, stage, result_ref, transport=transport)
    return updated, store.put_json(envelope_uri, updated)
