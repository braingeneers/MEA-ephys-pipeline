"""Prepare source-bound Maxwell task plans from an existing artifact envelope."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from ephys_modular.contracts import (ContractError, EPHYS, MAX_JSON_BYTES, assert_within,
    canonical_json, decode_json, digest, manifest_uri, selected_stage, stage_output_uri,
    stage_signature, validate_root)
from ephys_modular.local import LocalStore
from ephys_modular.publication import load_envelope, load_stage
from ephys_modular.s3 import S3Error, S3Store
from conversion_allocation import conversion_allocation, scientific_settings
from inspect_maxwell_input import inspect_input


def _projection_metadata_size(value):
    if (not isinstance(value, dict) or type(value.get('projection_schema_version')) not in (int, float)
            or value['projection_schema_version'] != 1):
        raise ContractError('Unsupported NWB metadata projection schema')
    # The maintained writer validates the actual typed graph. Its isolated NWB
    # contains metadata only, never a fabricated ElectricalSeries or acquisition.
    from cell_culture_projection import apply_cell_culture_projection, validate_projection_export
    from convert_maxone_to_nwb import parse_iso8601
    import ndx_cell_culture  # register exact owned typed extension
    from pynwb import NWBFile, NWBHDF5IO
    validate_projection_export(value)
    context, common = value.get('context') or {}, value.get('common') or {}
    if not isinstance(context, dict) or not isinstance(common, dict):
        raise ContractError('Projection context/common must be objects')
    date = (parse_iso8601(str(context['session_start_time'])) if context.get('session_start_time')
            else datetime(2000, 1, 1, tzinfo=timezone.utc))
    nwb = NWBFile(session_description=str(context.get('session_description') or common.get('experiment_description')
                                         or 'Metadata allocation validation'),
                  identifier='metadata-allocation', session_start_time=date)
    for name in ('lab', 'institution', 'experiment_description', 'keywords', 'related_publications', 'notes', 'protocol'):
        if common.get(name) not in (None, '', [], {}):
            setattr(nwb, name, common[name])
    nwb.session_id = str(context.get('session_id') or f"{value.get('experiment_name')}:{value.get('unit')}")
    apply_cell_culture_projection(nwb, copy.deepcopy(value))
    with tempfile.TemporaryDirectory(prefix='maxwell-projection-plan-') as temporary:
        path = Path(temporary) / 'metadata-only.nwb'
        with NWBHDF5IO(str(path), 'w') as output:
            output.write(nwb)
        return path.stat().st_size


def _prepare_fresh_task(store, unit, source, selector, output, effective, signature,
                        context, *, metadata_read_budget_bytes, read_budgets):
    """Construct the maintained physical-source task after caller graph admission.

    The source can be independently verified raw/cache/recovery bytes. This
    internal constructor neither asserts a source transition nor publishes it;
    the owning plan validator must establish its actual logical source binding.
    """
    transport = getattr(store, 'transport', 's3')
    root = context['output_base_uri']
    run_id, step_id = context['run_id'], context['step_id']
    for reference in unit['sources']:
        store.verify(reference)
    projection_size = projection_nwb_size = 0
    if 'projection' in unit:
        projection = unit['projection']
        value, _ = store.read_json(projection['uri'], expected=projection)
        projection_size = projection['size']
        projection_nwb_size = _projection_metadata_size(value)
    if 'target' in unit:
        store.read_json(unit['target']['uri'], expected=unit['target'])
    header = inspect_input(store, source, [selector], read_budgets=read_budgets,
                           metadata_read_budget_bytes=metadata_read_budget_bytes)
    allocation = conversion_allocation(source, header['header'], effective,
        projection_bytes=projection_size, projection_nwb_metadata_bytes=projection_nwb_size,
        minimum_disk_gib=context.get('runtime', {}).get('minimum_disk_gib', 0),
        publication_transport=transport)
    destination = stage_output_uri(output, 'conversion', signature, run_id, step_id, transport=transport)
    paths = {'nwb': destination + 'output.nwb', 'lindi': destination + 'output.nwb.lindi.json',
             'validation': destination + 'output.nwb.validation.json',
             'verification': destination + 'conversion-verification.json'}
    if effective['create_linked_maxwell_companion']:
        paths['companion'] = destination + 'output.linked.raw.h5'
    for uri in paths.values():
        assert_within(uri, root, transport=transport)
    return {'unit': copy.deepcopy(unit), 'raw': source,
        'raw_dataset_path': header['header']['selected'][0]['raw_dataset_path'],
        'header': header, 'allocation': allocation, 'outputs': paths}


def prepare_conversion(store, artifacts, submitted, context, owner, *,
                       metadata_read_budget_bytes=64 * 1024 * 1024, read_budgets=None):
    """Validate input graph and return plans; perform no durable mutation or science.

    Raw stages (`selection` or selected `split`) emit one `maxwell_raw` artifact
    per unit. That exact object must appear in the unit's `sources`. Unit metadata
    owns `raw_dataset_path` and an explicit `unit_output_uri` beneath the reviewed
    root. All undeclared recording/unit metadata is retained unchanged.
    """
    transport = getattr(store, 'transport', 's3')
    context, owner, submitted = copy.deepcopy(context), copy.deepcopy(owner), copy.deepcopy(submitted)
    root = context['output_base_uri']
    validate_root(root, transport)
    if store.output_root is not None and store.output_root != root:
        raise ContractError('Context output root differs from the reviewed store root')
    run_id, step_id = context['run_id'], context['step_id']
    # Validate identifiers, root, owner and scientific values before source reads.
    manifest_uri(root, run_id, step_id, transport=transport)
    from ephys_modular.contracts import _shape
    _shape(owner, 'owner')
    if owner['workflow_id'] != 'braingeneers-modular-maxwell-to-nwb':
        raise ContractError('Task plans require the owning Maxwell workflow identity')
    if context.get('workflow_version', owner['workflow_version']) != owner['workflow_version']:
        raise ContractError('Context version differs from the owning workflow')
    from owner_runtime import validate_owner_runtime
    validate_owner_runtime(owner)
    effective = scientific_settings(submitted)
    if effective['session_start_time']:
        from convert_maxone_to_nwb import parse_iso8601
        parse_iso8601(effective['session_start_time'])
    envelope, parent = load_envelope(store, artifacts, root)
    if envelope['contract'] != EPHYS:
        raise ContractError('Maxwell conversion requires the Ephys artifact contract')
    store.read_json(envelope['selection']['uri'], expected=envelope['selection'])
    kind = 'split' if 'split' in envelope['selected'] else 'selection'
    upstream = selected_stage(envelope, kind, transport=transport)
    raw_stage = load_stage(store, upstream, EPHYS, root, envelope['selection'])
    outcomes = {item['unit_id']: item for item in raw_stage['outcomes']}
    bindings, destinations = [], set()
    for unit in raw_stage['unit_inputs']:
        outcome = outcomes[unit['unit_id']]
        raw = [item['object'] for item in outcome['artifacts'] if item['kind'] == 'maxwell_raw']
        if outcome['status'] != 'complete' or len(raw) != 1 or raw[0] not in unit['sources']:
            raise ContractError('Raw stage must bind one complete maxwell_raw object to each unit input')
        metadata = unit['metadata']
        selector, output = metadata.get('raw_dataset_path'), metadata.get('unit_output_uri')
        if not isinstance(selector, str) or not selector.strip():
            raise ContractError('Raw stage must declare an explicit raw_dataset_path selector')
        validate_root(output, transport)
        assert_within(output + 'planned-object', root, transport=transport)
        if output in destinations:
            raise ContractError('Selected units cannot share an output prefix')
        destinations.add(output)
        configured = effective['maxwell_raw_dataset_path']
        if configured.strip().lower() != 'auto' and configured != selector:
            raise ContractError('Scientific selector conflicts with the artifact-selected raw table')
        bindings.append((unit, raw[0], selector, output))
    inputs = copy.deepcopy(raw_stage['unit_inputs'])
    semantic = {'contract': EPHYS, 'kind': 'conversion', 'variant': step_id,
        'dataset_uri': root, 'selection': envelope['selection'], 'owner': owner,
        'settings': {'submitted': submitted, 'effective': effective},
        'inputs': [upstream], 'unit_inputs': inputs}
    signature = stage_signature(semantic)
    tasks = []
    # Validate every binding before mutation; this API only reads verified inputs
    # and a temporary metadata-only NWB used by the existing projection writer.
    for unit, source, selector, output in bindings:
        tasks.append(_prepare_fresh_task(store, unit, source, selector, output, effective,
            signature, context, metadata_read_budget_bytes=metadata_read_budget_bytes,
            read_budgets=read_budgets))
    plan = {'schema_version': 1, 'document_type': 'maxwell_conversion_plan',
            'parent': parent, 'semantic': semantic, 'stage_signature': signature,
            'execution': {'run_id': run_id, 'step_id': step_id}, 'tasks': tasks,
            'planning_options': {'metadata_read_budget_bytes': metadata_read_budget_bytes,
                'read_budgets': copy.deepcopy(read_budgets or {}),
                'minimum_disk_gib': context.get('runtime', {}).get('minimum_disk_gib', 0)},
            'scientific_execution_complete': False}
    plan['plan_digest'] = digest(plan)
    canonical_json(plan)
    return plan


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request-file', required=True)
    parser.add_argument('--output-file', required=True)
    parser.add_argument('--transport', choices=('s3', 'local'), default='s3')
    parser.add_argument('--s3-endpoint')
    parser.add_argument('--aws-profile')
    args = parser.parse_args()
    with open(args.request_file, 'rb') as stream:
        request = decode_json(stream.read(MAX_JSON_BYTES + 1))
    root = request['context']['output_base_uri']
    validate_root(root, args.transport)
    store = (LocalStore if args.transport == 'local' else S3Store)(
        output_root=root, endpoint=args.s3_endpoint, profile=args.aws_profile)
    plan = prepare_conversion(store, request['artifacts'], request['settings'], request['context'], request['owner'])
    target = Path(os.path.abspath(args.output_file))
    reference = LocalStore(target.parent.as_uri() + '/').put_json(target.as_uri(), plan)
    print(reference['uri'])


if __name__ == '__main__':
    try:
        main()
    except S3Error as error:
        import sys
        print('[task_preparation] ' + str(error), file=sys.stderr)
        raise SystemExit(error.exit_code) from None
    except (ContractError, ValueError) as error:
        import sys
        print('[task_preparation] ' + str(error), file=sys.stderr)
        raise SystemExit(2) from None
