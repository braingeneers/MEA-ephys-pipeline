#!/usr/bin/env python3
"""Exercise atomic manifest replacement, permissions and failure preservation."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'pipeline/helpers'))
import portable_bundle as portable


def check(output, manifest=None):
    output.mkdir(parents=True, exist_ok=False)
    value = json.loads(manifest.read_text()) if manifest else {
        'metadata': {'typed': [7, 7.0, -0.0, None, False], 'text': 'x' * 14000}}
    changed = deepcopy(value)
    changed['publication_probe'] = True
    payloads = [portable.encoded(row) + b'\n' for row in (value, changed)]
    path = output / 'bundle.json'
    ordinary = output / 'ordinary-mode'
    ordinary.write_bytes(payloads[0])
    portable.write_manifest(path, value)
    assert path.read_bytes() == payloads[0]
    assert path.stat().st_mode & 0o777 == ordinary.stat().st_mode & 0o777
    for operation in ('replace', 'fsync'):
        for candidate in (path, output / ('new-' + operation + '.json')):
            baseline = candidate.read_bytes() if candidate.exists() else None
            with patch.object(portable.os, operation, side_effect=OSError('injected ' + operation)):
                try:
                    portable.write_manifest(candidate, changed)
                except OSError as error:
                    assert str(error) == 'injected ' + operation
                else:
                    raise AssertionError('Publication failure was suppressed')
            assert (candidate.read_bytes() if candidate.exists() else None) == baseline
            assert not list(output.glob('.*.tmp'))
    stop = threading.Event()
    ready = threading.Barrier(3)
    errors, observations = [], []

    def reader():
        try:
            assert path.read_bytes() in payloads
            ready.wait(timeout=10)
            count = 1
            while not stop.is_set():
                assert path.read_bytes() in payloads, 'Reader observed partial/empty manifest'
                count += 1
            observations.append(count)
        except BaseException as error:
            errors.append(repr(error))

    readers = [threading.Thread(target=reader) for _ in range(2)]
    for thread in readers:
        thread.start()
    ready.wait(timeout=10)
    try:
        for i in range(100):
            portable.write_manifest(path, (value, changed)[i % 2])
    finally:
        stop.set()
        for thread in readers:
            thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in readers)
    assert not errors, errors
    assert len(observations) == 2 and min(observations) > 0
    assert not list(output.glob('.*.tmp'))
    result = dict(passed=True, reader_observations=observations,
                  complete_bytes_only=True, failed_replacement_preserves_prior=True,
                  failed_flush_preserves_prior=True, ordinary_permissions_preserved=True,
                  canonical_bytes_unchanged=True)
    (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args()
    check(args.output, args.manifest)
