"""Small Nextflow adapter; maintained helpers own science and publication."""
import argparse
from pathlib import Path
import sys
import time

from ephys_modular.contracts import ContractError, MAX_JSON_BYTES, canonical_json, decode_json
from ephys_modular.local import LocalStore
from ephys_modular.s3 import S3Error, S3Store
from conversion_worker import ScientificFailure, commit_conversion, execute_unit
from raw_selection import publish_raw_selection
from task_preparation import prepare_conversion


def read(path):
    with Path(path).open('rb') as stream:
        return decode_json(stream.read(MAX_JSON_BYTES + 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('raw', 'prepare', 'unit', 'commit'))
    parser.add_argument('--plan-file')
    parser.add_argument('--outcome-file', action='append', default=[])
    args = parser.parse_args()
    request = decode_json(sys.stdin.buffer.read(MAX_JSON_BYTES + 1))
    meta = request['meta']
    runtime, context = meta['runtime'], meta['context']
    root = context['output_base_uri']
    store = (LocalStore if meta['transport'] == 'local' else S3Store)(
        output_root=root, endpoint=runtime.get('s3_endpoint') or None,
        profile=runtime.get('aws_profile') or None)
    start = time.monotonic()
    if args.operation == 'raw':
        # Scientific values are checked before creating the selection graph too.
        from conversion_allocation import scientific_settings
        scientific_settings(meta['settings'])
        result = publish_raw_selection(store, request['recordings'], context, meta['owner'])
    elif args.operation == 'prepare':
        plan = prepare_conversion(store, request['artifacts'], meta['settings'], context, meta['owner'])
        Path('conversion-plan.json').write_bytes(canonical_json(plan))
        result = None
    elif args.operation == 'unit':
        result = execute_unit(store, read(args.plan_file), meta['unit_id'], 'scientific-task',
                              public_endpoint=runtime.get('public_reader_endpoint') or None)
        Path('outcome-' + meta['unit_id'] + '.json').write_bytes(canonical_json(result))
    else:
        result = commit_conversion(store, read(args.plan_file), [read(path) for path in args.outcome_file])
    if args.operation in ('raw', 'commit'):
        Path('artifact-uri.txt').write_text(result['uri'] + '\n')
        Path('result.txt').write_text('artifacts_uri=' + result['uri'] + '\n')
    Path('native-task-evidence.json').write_bytes(canonical_json({
        'operation': args.operation, 'owner': meta['owner'], 'execution': {
            key: context[key] for key in ('run_id', 'step_id')},
        'submitted': meta['settings'], 'runtime': runtime,
        'requested': request['resources'], 'attempt': request['attempt'],
        'elapsed_seconds': time.monotonic() - start, 'exit': 0,
        'helper_path': str(Path(__file__).resolve()),
    }))


if __name__ == '__main__':
    try:
        main()
    except (S3Error, ScientificFailure, ContractError) as error:
        print('[native_task] ' + str(error), file=sys.stderr)
        raise SystemExit(getattr(error, 'exit_code', 2)) from None
