#!/usr/bin/env python3
"""Exercise the real native S3 converter and local bridge on supplied tiny inputs.

Run in the pinned native image with its exported runtime/common on PYTHONPATH.
Use only a disposable S3 server/bucket. The complete submitted recordings and
bindings must refer to local fixtures; no fixture or credentials are committed.
This checks transport integration, not original-vs-modular scientific parity.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / 'pipeline/helpers'))
import maxwell_bridge as bridge
from conversion_worker import execute_unit
from raw_selection import publish_raw_selection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recordings', type=Path, required=True)
    parser.add_argument('--bindings', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--profile')
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--without-companion', action='store_true')
    parser.add_argument('--reuse-native', action='store_true',
                        help='Recheck the already committed fixture without rerunning conversion')
    args = parser.parse_args()
    endpoint = urlsplit(args.endpoint)
    if endpoint.scheme != 'http' or endpoint.hostname not in {'s3', 'localhost', '127.0.0.1'}:
        parser.error('This test permits only a disposable local HTTP S3 endpoint')
    args.output.mkdir(parents=True, exist_ok=args.reuse_native)
    original_records = bridge.read_json(args.recordings)
    records, bindings = deepcopy(original_records), bridge.read_json(args.bindings)
    root = 's3://' + args.bucket + '/fixture/'
    store = bridge.S3Store(root, endpoint=args.endpoint, profile=args.profile)
    store.client.create_bucket(Bucket=args.bucket)
    store.client.put_bucket_policy(Bucket=args.bucket, Policy=json.dumps({
        'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': '*',
            'Action': 's3:GetObject', 'Resource': 'arn:aws:s3:::' + args.bucket + '/fixture/*'}]}))
    sources, translations = {}, {}
    for index, record in enumerate(records):
        path = bridge.parse_file(record['input_uri'])
        sources[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        target = root + 'inputs/' + str(index) + '/recording.raw.h5'
        store.upload_file(target, path)
        translations[record['input_uri']] = record['input_uri'] = target
        for name, projection in record.get('nwb_metadata', {}).items():
            path = bridge.parse_file(projection['uri'])
            sources[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            projection['uri'] = root + 'inputs/' + str(index) + '/' + name + '.json'
            store.upload_file(projection['uri'], path)
    for binding in bindings:
        binding['input_uri'] = translations[binding['input_uri']]
    owner_root = SOURCE / 'pipeline/components/maxwell'
    image = bridge.read_json(owner_root / 'image.json')['pinned_reference']
    native_version = re.search(r"(?m)^\s*version\s*=\s*['\"]([^'\"]+)",
                               (owner_root / 'nextflow.config').read_text())[1]
    wrapper_version = re.search(r"(?m)^\s*version\s*=\s*['\"]([^'\"]+)",
                                (SOURCE / 'pipeline/nextflow.config').read_text())[1]
    owner = dict(workflow_id='braingeneers-modular-maxwell-to-nwb',
        workflow_version=native_version, interface_version='1', image=image,
        maintainer='Ephys workflow maintainers')
    runtime = dict(s3_endpoint=args.endpoint, public_reader_endpoint=args.endpoint)
    if args.profile:
        runtime['aws_profile'] = args.profile
    context = dict(output_base_uri=root, output_uri=root, run_id='s3-fixture',
                   step_id='convert', workflow_version=native_version, runtime=runtime)
    settings = {'create_linked_maxwell_companion': False} if args.without_companion else {}
    if args.reuse_native:
        from ephys_modular.contracts import manifest_uri
        _, native = store.read_json(manifest_uri(root, 's3-fixture', 'convert'))
    else:
        raw = publish_raw_selection(store, records, context | dict(
            step_id='convert-raw', collection_uri=root + 'derived/nwb/'), owner)
        native_plan = bridge.prepare_conversion(store, raw, settings, context, owner)
        outcomes = []
        for task in native_plan['tasks']:
            print('Converting ' + task['unit']['unit_id'], flush=True)
            outcomes.append(execute_unit(store, native_plan, task['unit']['unit_id'],
                args.output / ('native-' + task['unit']['unit_id']), public_endpoint=args.endpoint))
        native = bridge.commit_conversion(store, native_plan, outcomes)
    distribution = bridge.read_json(owner_root / 'export-manifest.json')
    local_root = args.output.as_uri() + '/'
    request = dict(native=dict(context=context, owner=owner,
        defaults=bridge.read_json(owner_root / 'defaults.json'), distribution={
            key: distribution[key] for key in ('source_commit', 'content_sha256')}),
        wrapper=dict(owner=dict(workflow_id='MEA-ephys-pipeline', workflow_version=wrapper_version),
            local_output_uri=local_root, publication_root=local_root + 'bridge/s3-fixture/convert/',
            unit_bindings=bindings), settings=settings, recordings=records)
    plan = bridge.prepare(native, request)
    for name, value in [('request', request), ('bridge-plan', plan)]:
        (args.output / (name + '.json')).write_bytes(bridge.canonical_json(value))
    assert bridge.json_equal(plan['source_runtime'], {
        key: value for key, value in runtime.items() if key in ('s3_endpoint', 'aws_profile')})
    assert bridge.json_equal(plan, bridge.prepare(native, request))
    units, ranges = [], 0
    for row in plan['units']:
        unit_id = row['unit_meta']['unit_id']
        unit = args.output / 'materialized' / unit_id / 'unit'
        unit.parent.mkdir(parents=True, exist_ok=True)
        if not args.reuse_native or not unit.exists():
            bridge.materialize(plan, unit_id, unit, row['requested_bytes'])
        bridge.verify_local(unit, (plan['digest'], row))
        assert ('output.linked.raw.h5' in row['files']) is (not args.without_companion)
        assert bridge.json_equal(row['evidence']['conversion/recordings.json'], records)
        units.append(unit)
        sidecar = bridge.read_json(unit / 'output.nwb.lindi.json')
        nwb = row['files']['output.nwb']
        for ref in sidecar['refs'].values():
            if not isinstance(ref, list) or len(ref) != 3:
                continue
            url, offset, length = ref
            for key, value in sidecar.get('templates', {}).items():
                url = url.replace('{{' + key + '}}', value)
            if not url.startswith(('http://', 'https://')):
                continue
            with urlopen(Request(url, headers={'Range': f'bytes={offset}-{offset+length-1}'})) as response:
                assert response.status == 206
                assert response.read() == store.read_range(nwb, offset, length)
            ranges += 1
    assert ranges > 0
    completed = bridge.commit(plan, units)
    assert completed['complete'] is True and len(completed['units']) == len(bindings)
    readonly = bridge.PublishedS3Only(root, endpoint=args.endpoint, profile=args.profile)
    for uri, value in [(root + 'missing.json', {}), (native['uri'], {'changed': True})]:
        try:
            readonly.put_json(uri, value)
        except (bridge.ContractError, bridge.S3Error):
            pass
        else:
            raise AssertionError('Bridge repaired a native publication')
    assert store.read_json(root + 'missing.json', optional=True) is None
    for field, changed in [('uri', root + 'missing.json'), ('sha256', '0' * 64)]:
        reference = dict(native, **{field: changed})
        try:
            bridge.prepare(reference, request)
        except (bridge.ContractError, bridge.S3Error):
            pass
        else:
            raise AssertionError('Bridge accepted missing or changed native evidence')
    assert store.read_json(root + 'missing.json', optional=True) is None
    for key, changed in [('run_id', 'another-run'), ('step_id', 'another-step')]:
        wrong = deepcopy(request)
        wrong['native']['context'][key] = changed
        wrong['wrapper']['publication_root'] = local_root + 'bridge/' + wrong['native']['context']['run_id'] + '/' + wrong['native']['context']['step_id'] + '/'
        try:
            bridge.prepare(native, wrong)
        except bridge.ContractError:
            pass
        else:
            raise AssertionError('Bridge accepted another execution')
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha for path, sha in sources.items())
    for name, value in [('request', request), ('bridge-plan', plan), ('completion', completed)]:
        (args.output / (name + '.json')).write_bytes(bridge.canonical_json(value))
    (args.output / 'result.json').write_text(json.dumps(dict(passed=True, units=len(units),
        http_ranges=ranges, companion=not args.without_companion,
        source_files_unchanged=len(sources), native_version=native_version,
        wrapper_version=wrapper_version, scientific_parity_claimed=False), indent=2) + '\n')


if __name__ == '__main__':
    main()
