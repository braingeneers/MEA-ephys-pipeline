"""Discoverable references to finalization evidence stored in another output root.

These references locate evidence; the scientific owner still verifies the entire
source lifetime and conversion. Keys bind the existing physical-source marker so
different finalizations never overwrite one another's provenance.
"""
from .contracts import ContractError, _shape, validate_ref, validate_root
from .discovery import read_json_bounded


def dataset_root(source_uri):
    """Use the established dataset layout, without guessing arbitrary input roots."""
    if not isinstance(source_uri, str) or '/original/data/' not in source_uri:
        return None
    root = source_uri.split('/original/data/', 1)[0] + '/'
    validate_root(root, 'local' if root.startswith('file:') else 's3')
    return root


def reference_uri(source_uri, recording_id, signature):
    _shape(recording_id, 'identifier')
    _shape(signature, 'digest')
    root = dataset_root(source_uri)
    return None if root is None else (root + 'derived/manifests/source-finalization/'
                                     + recording_id + '/' + signature + '.ref.json')


def _binding(value, source_uri, recording_id, signature):
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value['schema_version'] not in (2, 3, 4)
            or value.get('status') != 'complete' or value.get('finalize_sources') is not True
            or value.get('source_uri') != source_uri or value.get('recording_id') != recording_id
            or value.get('signature') != signature or value.get('pending_raw_dataset_paths', []) != []):
        raise ContractError('Finalization reference does not identify this completed source transaction')


def read_reference(store, source_uri, recording_id, signature):
    """Only an absent locator is optional; a declared missing/changed target fails."""
    uri = reference_uri(source_uri, recording_id, signature)
    if uri is None:
        return None
    found = read_json_bounded(store, uri, optional=True)
    if found is None:
        return None
    reference, _ = found
    validate_ref(reference, strong=True, transport=store.transport)
    value, observed = read_json_bounded(store, reference['uri'], expected=reference)
    _binding(value, source_uri, recording_id, signature)
    return value, observed


def publish_reference(source_store, finalization):
    """Publish after owner validation, using an explicitly source-scoped writer.

    No scientific object or existing completion is created, updated or repaired.
    The caller owns full scientific/source validation before invoking this helper.
    """
    validate_ref(finalization, strong=True, transport=source_store.transport)
    value, observed = read_json_bounded(source_store, finalization['uri'], expected=finalization)
    if not isinstance(value, dict):
        raise ContractError('Finalization evidence must be an object')
    source, identity, signature = (value.get(k) for k in ('source_uri', 'recording_id', 'signature'))
    _binding(value, source, identity, signature)
    uri = reference_uri(source, identity, signature)
    if uri is None or source_store.output_root != dataset_root(source):
        raise ContractError('Finalization reference requires the explicit original dataset writer')
    published = source_store.put_json(uri, observed)
    source_store.read_json(uri, expected=published)
    return published
