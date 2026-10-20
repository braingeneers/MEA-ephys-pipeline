#!/usr/bin/env python3
"""Create a test-only v2 counterpart from independently verified immutable v1 data.

This is deliberately outside production helpers/entry points. Explicit immutable
authority, reader identities and path mappings are mandatory. It never upgrades
an original artifact or attributes historical science to the new packager.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / 'pipeline/helpers'))
import portable_bundle as portable
import stage_bundle as stages


def require(condition, message):
    if not condition:
        raise ValueError(message)


def identity(path):
    return dict(bytes=path.stat().st_size, sha256=portable.sha256(path))


def read(path):
    return json.loads(path.read_text())


def run(args):
    start = time.monotonic()
    require(portable.sha256(args.authority) == args.authority_sha256,
            'Immutable corpus authority digest differs')
    require(portable.sha256(args.legacy_source_files) == args.legacy_source_files_sha256,
            'Frozen legacy source inventory digest differs')
    authority = read(args.authority)
    require(authority.get('passed') is True, 'Original corpus authority was not accepted')
    matches = [item for item in authority['corpus'] if item['label'] == args.label]
    require(len(matches) == 1, 'Original label must select exactly one verified corpus entry')
    entry = matches[0]
    mapping = read(args.path_map)
    require(isinstance(mapping, dict) and mapping, 'Explicit authority path mapping is required')

    def mapped(value):
        for prefix in sorted(mapping, key=len, reverse=True):
            try:
                suffix = Path(value).relative_to(prefix)
            except ValueError:
                continue
            root = Path(mapping[prefix]).resolve(strict=True)
            result = (root / suffix).resolve(strict=True)
            require(result.is_relative_to(root), 'Authority path escapes its declared mapping')
            return result
        raise ValueError('Unmapped original authority path: ' + value)

    original = mapped(entry['root'])
    scientific_authority = mapped(entry['authority']['path'])
    for path in [original, scientific_authority, args.authority, args.legacy_source,
                 args.legacy_source_files, args.path_map]:
        require(bool(os.statvfs(path).f_flag & os.ST_RDONLY), 'Original authorities/readers must be mounted read-only')
    original_files = portable.inventory(original / 'data')
    require(portable.encoded(original_files) == portable.encoded(entry['files']), 'Original complete payload differs')
    require(identity(original / 'bundle.json') == {k: v for k, v in entry['manifest'].items() if k != 'path'},
            'Original manifest differs')
    require(identity(scientific_authority) == {k: v for k, v in entry['authority'].items() if k != 'path'},
            'Preexisting independent scientific authority differs')
    legacy_expected = read(args.legacy_source_files)
    legacy_actual = {f.relative_to(args.legacy_source).as_posix(): identity(f)
                     for f in args.legacy_source.rglob('*') if f.is_file()}
    require(portable.encoded(legacy_actual) == portable.encoded(legacy_expected), 'Frozen legacy reader source differs')
    record = stages.read_record(original)
    require(type(record.get('schema_version')) is int and record['schema_version'] == 1,
            'Only exact original v1 artifacts are accepted by this test importer')
    require(portable.encoded(record['metadata']) == portable.encoded(entry['metadata']),
            'Original scientific provenance differs from the corpus authority')
    require(not args.output.exists() and not args.report.exists(), 'Import output/report already exists')
    require(not args.report.resolve().is_relative_to(args.output.resolve()), 'Import report must stay outside the counterpart')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    legacy_command = [sys.executable, str(args.legacy_source / 'pipeline/helpers/stage_bundle.py'),
                      '--outcomes-code', str(args.legacy_source / 'capsules/kilosort4/code/outcomes.py'),
                      'verify', '--bundle', str(original), '--kind', record['stage'], '--unit-id', record['unit_id']]
    legacy = subprocess.run(legacy_command, text=True, capture_output=True)
    (args.report.parent / (args.label + '-legacy.stdout')).write_text(legacy.stdout)
    (args.report.parent / (args.label + '-legacy.stderr')).write_text(legacy.stderr)
    require(legacy.returncode == 0, 'Frozen original verifier rejected the original artifact')
    print(args.label, 'frozen legacy CLI passed; reading complete original values', flush=True)
    # Derive complete expectations from the independently authorized original
    # before creating any counterpart or mutable control.
    expected = [portable.fingerprint(item, original, portable=True) for item in record['serializations']]
    before = dict(label=args.label, corpus_authority=identity(args.authority), original_manifest=identity(original / 'bundle.json'),
                  scientific_authority=entry['authority'], legacy_command=legacy_command, legacy_exit=legacy.returncode,
                  legacy_source_inventory=identity(args.legacy_source_files), original_manifest_record=record,
                  complete_original_scientific_fingerprints=expected, new_scientific_execution=False)
    args.report.with_suffix('.before.json').write_bytes(portable.encoded(before) + b'\n')
    print(args.label, 'complete original expectations frozen; packaging counterpart', flush=True)
    descriptors = [dict(item, source=str(original / item['path']), base=str(original / item.get('relative_to', 'data')))
                   for item in record['serializations']]
    spec = dict(schema_version=1, stage=record['stage'], unit_id=record['unit_id'], recording_ids=record['recording_ids'],
                metadata=deepcopy(record['metadata']), payloads=[dict(source=str(original / 'data'), destination='data')],
                serializations=descriptors)
    result = portable.pack(spec, args.output)
    print(args.label, 'original/relocated values matched; checking current stage API', flush=True)
    require(portable.encoded(result['scientific_fingerprints']) == portable.encoded(expected),
            'Counterpart complete scientific values differ from the original')
    # Keep historical capsule serialization provenance; the new packager's
    # immediate-input identities are recorded separately, never as new science.
    packaging_serializations = result['original_serializations']
    result['original_serializations'] = deepcopy(record['original_serializations'])
    result.pop('content_sha256')
    result['content_sha256'] = hashlib.sha256(portable.encoded(result)).hexdigest()
    (args.output / 'bundle.json').write_bytes(portable.encoded(result) + b'\n')
    require(portable.encoded(portable.inventory(args.output / 'data')) == portable.encoded(original_files),
            'Counterpart payload bytes changed; retain the attempt for an explicit serialization/science audit')
    classifier = stages.classifier(SOURCE / 'capsules/kilosort4/code/outcomes.py')
    verified = stages.verify(args.output, record['stage'], record['unit_id'], classifier,
                             unit_meta=record['metadata']['unit_meta'])
    require(portable.encoded(verified) == portable.encoded(result), 'Current API returned a different receipt')
    require(portable.encoded(portable.inventory(original / 'data')) == portable.encoded(original_files),
            'Original payload changed during import')
    require(identity(original / 'bundle.json') == before['original_manifest'], 'Original manifest changed during import')
    report = dict(passed=True, label=args.label, scope='Explicit QA packaging only; original independent science retains its exact authority scope',
                  original_root=str(original), original_manifest=before['original_manifest'], scientific_authority=entry['authority'],
                  corpus_authority=before['corpus_authority'], legacy_CLI_reader_passed=True, current_API_passed=True,
                  output=str(args.output), new_manifest=identity(args.output / 'bundle.json'), contract=portable.CONTRACT,
                  original_scientific_metadata_and_serialization_provenance_preserved=True,
                  new_packaging_immediate_input_serializations=packaging_serializations,
                  all_payload_files_byte_identical=True, files=len(original_files), payload_bytes=result['bytes'],
                  full_original_posthash_exact=True, complete_original_and_counterpart_scientific_values_exact=True,
                  scientific_producer_rerun=False, elapsed_seconds=time.monotonic() - start)
    args.report.write_bytes(portable.encoded(report) + b'\n')
    print(json.dumps(dict(passed=True, label=args.label, files=len(original_files), seconds=report['elapsed_seconds'])), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authority', type=Path, required=True)
    parser.add_argument('--authority-sha256', required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--path-map', type=Path, required=True)
    parser.add_argument('--legacy-source', type=Path, required=True)
    parser.add_argument('--legacy-source-files', type=Path, required=True)
    parser.add_argument('--legacy-source-files-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
