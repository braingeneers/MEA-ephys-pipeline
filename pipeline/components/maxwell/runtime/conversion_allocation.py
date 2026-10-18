"""Owning conversion allocation model; no compressed-size inference or science defaults."""
import json
import math
from pathlib import Path

import numpy as np
import workflow_defaults
from ephys_modular.contracts import ContractError, digest

GIB = 1024 ** 3


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ContractError(label + ' must be an integer >= ' + str(minimum))
    return value


def _ceil_div(numerator, denominator):
    return (numerator + denominator - 1) // denominator


def allocation_policy():
    return json.loads((Path(__file__).parent / 'allocation-policy.json').read_text())


def _optional_integer(value, label, minimum):
    if type(value) is bool or not isinstance(value, (int, str)):
        raise ContractError(label + ' must be an integer or none')
    if isinstance(value, str) and value.strip().lower() in ('none', 'null', ''):
        return
    try:
        parsed = int(value)
    except ValueError:
        raise ContractError(label + ' must be an integer or none') from None
    if parsed < minimum:
        raise ContractError(label + ' is below its allowed minimum')


def scientific_settings(submitted):
    if not isinstance(submitted, dict):
        raise ContractError('Scientific settings must be an object')
    effective = workflow_defaults.scientific_defaults()
    if set(submitted) - set(effective):
        raise ContractError('Unknown scientific setting; resources and input metadata are separate')
    effective.update(submitted)
    duration = effective['nwb_chunk_duration_ms']
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ContractError('nwb_chunk_duration_ms must be a finite positive number')
    _integer(effective['nwb_chunk_channels'], 'nwb_chunk_channels')
    _integer(effective['nwb_buffer_chunks'], 'nwb_buffer_chunks', 1)
    _optional_integer(effective['lindi_contiguous_dataset_max_chunk_size'], 'lindi_contiguous_dataset_max_chunk_size', 1)
    _optional_integer(effective['lindi_num_dataset_chunks_threshold'], 'lindi_num_dataset_chunks_threshold', 0)
    if type(effective['create_linked_maxwell_companion']) is not bool:
        raise ContractError('create_linked_maxwell_companion must be boolean')
    for key in ('session_description', 'session_identifier_prefix', 'session_start_time',
                'subject_id', 'subject_species', 'subject_description', 'maxwell_raw_dataset_path'):
        if not isinstance(effective[key], str):
            raise ContractError(key + ' must be a string')
    if not effective['maxwell_raw_dataset_path'].strip():
        raise ContractError('maxwell_raw_dataset_path must be explicit or auto')
    # JSON validation preserves exact submitted numeric types without mutating
    # dictionaries/defaults or normalizing submitted {} into explicit defaults.
    from ephys_modular.contracts import canonical_json
    canonical_json(effective)
    return effective


def conversion_allocation(reference, header, effective, *, projection_bytes=0,
                          projection_nwb_metadata_bytes=0, minimum_disk_gib=0,
                          publication_transport='s3'):
    """Account every simultaneous file plus explicit, versioned format allowances.

    These allowances are allocation policy, not a universal bound on future tools.
    The execution worker must enforce plan/free-space guards before publication.
    """
    if publication_transport not in ('local', 's3'):
        raise ContractError('Publication transport must be explicitly local or s3')
    selected = header['selected']
    if len(selected) != 1:
        raise ContractError('One conversion allocation requires exactly one selected raw table')
    metadata = header.get('metadata_volume')
    if not isinstance(metadata, dict) or metadata.get('complete') is not True:
        raise ContractError('A complete measured metadata inventory is required for allocation')
    unit = selected[0]
    channels, frames = unit['shape']
    channels = _integer(channels, 'raw channels', 1)
    frames = _integer(frames, 'raw frames', 1)
    sampling = unit['sampling_hz']
    if type(sampling) not in (int, float) or not math.isfinite(sampling) or sampling <= 0:
        raise ContractError('A verified positive sample rate is required')
    chunk_samples = sampling * effective['nwb_chunk_duration_ms'] / 1000
    if not math.isfinite(chunk_samples):
        raise ContractError('Chunk duration overflows the scientific CLI sample calculation')
    samples = min(frames, max(1, int(round(chunk_samples))))
    chunk_channels = effective['nwb_chunk_channels'] or channels
    if chunk_channels > channels:
        raise ContractError('nwb_chunk_channels exceeds the verified source channel count')
    chunk_count = _ceil_div(frames, samples) * _ceil_div(channels, chunk_channels)
    # Include edge-chunk padding and at least uint16 output. Source rows are a
    # conservative channel bound when NeuroConv excludes counter/unrouted rows.
    padded_signal = chunk_count * samples * chunk_channels * max(2, np.dtype(unit['dtype']).itemsize)
    policy = allocation_policy()
    process = json.loads((Path(workflow_defaults.__file__).resolve().parents[1] / 'process-policy.json').read_text())
    metadata_payload = _integer(metadata['total_payload_bytes'], 'measured metadata payload')
    metadata_hdf5 = (_ceil_div(metadata_payload * (100 + policy['metadata_payload_headroom_percent']), 100)
        + (_integer(metadata['unique_objects'], 'metadata objects') + _integer(metadata['link_count'], 'metadata links'))
            * policy['hdf5_object_allowance_bytes']
        + _integer(metadata['attribute_count'], 'metadata attributes') * policy['hdf5_attribute_allowance_bytes']
        + _integer(metadata['variable_value_count'], 'variable values') * policy['hdf5_variable_value_allowance_bytes'])
    projection_bytes = _integer(projection_bytes, 'projection bytes')
    projection_nwb_metadata_bytes = _integer(projection_nwb_metadata_bytes, 'projection NWB metadata bytes')
    encoded_signal = (_ceil_div(padded_signal * (100 + policy['signal_payload_headroom_percent']), 100)
                      + chunk_count * policy['encoded_chunk_allowance_bytes'])
    nwb = encoded_signal + metadata_hdf5 + projection_nwb_metadata_bytes + policy['nwb_structure_allowance_bytes']
    companion = (metadata_hdf5 + policy['companion_structure_allowance_bytes']
                 if effective['create_linked_maxwell_companion'] else 0)
    lindi = (policy['lindi_structure_allowance_bytes']
        + chunk_count * policy['lindi_chunk_reference_allowance_bytes']
        + (metadata_hdf5 + projection_nwb_metadata_bytes) * policy['metadata_json_expansion_allowance'])
    components = {
        'staged_source': _integer(reference['size'], 'source size', 1),
        'staged_projection': projection_bytes,
        'compatibility_views': metadata_hdf5,
        'converted_nwb': nwb,
        'repacked_nwb': nwb,
        'compact_companion': companion,
        'lindi': lindi,
        'validation_and_evidence': policy['validation_report_allowance_bytes'],
    }
    # Local publication is a create-only copy. It can share the scratch filesystem
    # while both NWBs and all intermediates still exist; do not hide that extra copy.
    components['local_publication_copies'] = (nwb + companion + lindi
        + policy['validation_report_allowance_bytes'] if publication_transport == 'local' else 0)
    simultaneous = sum(components.values())
    floor = max(_integer(minimum_disk_gib, 'minimum_disk_gib'),
                max(int(str(item['disk']).split()[0]) for item in process['processes'].values()))
    reserve = process['minimum_free_gb'] * GIB
    with_headroom = _ceil_div(simultaneous * (100 + policy['workspace_headroom_percent']), 100) + reserve
    requested_gib = max(floor, _ceil_div(with_headroom, GIB))
    return {'schema_version': 1, 'model': 'maxwell-complete-files-v1',
            'publication_transport': publication_transport,
            'policy_digest': digest(policy), 'policy': policy,
            'source_physical_bytes': reference['size'],
            'selected_raw_logical_bytes': unit['logical_bytes'],
            'signal_chunk_shape_upper': [samples, chunk_channels], 'signal_chunks_upper': chunk_count,
            'padded_signal_payload_bytes': padded_signal,
            'components_bytes': components, 'simultaneous_bytes': simultaneous,
            'minimum_free_bytes': reserve, 'requested_gib': requested_gib,
            'requested_bytes': requested_gib * GIB, 'complete_scratch_plan': True,
            'requires_worker_usage_guards': True}
