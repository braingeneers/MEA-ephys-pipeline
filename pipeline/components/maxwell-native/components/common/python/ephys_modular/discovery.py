"""Bounded read-only discovery through the ordinary transport/retry owners."""
import os
from datetime import datetime
import json
from pathlib import Path
import time

from .contracts import ContractError, MAX_JSON_BYTES, assert_within, decode_json, parse_file, parse_s3, validate_root
from .s3 import IntegrityError, ObjectMissing


def optional_contents(store, uri):
    """One optional streamed GET, including its full body in the retry budget."""
    if uri.startswith('s3:'):
        remote = store if store.transport == 's3' else store._remote
        class Sink:
            def write(self, data):
                pass
        result = remote.policy.call('discover_optional_contents',
            lambda: remote._read_once(uri, optional=True, limit=None, sink=Sink()))
        return None if result is None else result[1]
    if store.transport != 'local':
        raise ContractError('File discovery requires explicit standalone local transport')
    try:
        return store.stat(uri)
    except ObjectMissing:
        return None


def current_object(store, uri):
    """Freeze the actual physical identity before enforcing historical bindings."""
    ref = store.stat(uri)
    return store.inspect_object(ref)


def read_json_bounded(store, uri, *, expected=None, optional=False, limit=MAX_JSON_BYTES):
    """Apply a remaining lookup byte budget before consuming the response body."""
    if expected is not None and optional:
        raise ContractError('A declared discovery reference is never optional')
    if type(limit) is not int or not 0 < limit <= MAX_JSON_BYTES:
        raise ContractError('Discovery JSON byte allowance must be positive and bounded')
    if uri.startswith('s3:'):
        remote = store if store.transport == 's3' else store._remote
        result = remote.policy.call('discover_json',
            lambda: remote._read_once(uri, expected=expected, optional=optional, limit=limit))
    else:
        if store.transport != 'local':
            raise ContractError('File discovery requires explicit standalone local transport')
        try:
            result = store._read_local(uri, expected=expected, limit=limit, collect=True)
        except ObjectMissing:
            if not optional:
                raise
            return None
    return None if result is None else (decode_json(result[0]), result[1])


def list_prefix(store, prefix, *, max_objects=10000, max_pages=20,
                max_bytes=64 * 1024**2, deadline=None):
    """Return a complete bounded listing, or fail; never return a truncated miss.

    Bounds include every visited entry, not only matching candidate filenames.
    Local traversal uses directory descriptors and rejects links/special files.
    S3 page retries use the store's one explicit budget with SDK retries disabled.
    """
    validate_root(prefix, store.transport)
    assert_within(prefix + 'discovery-scope', store.output_root, transport=store.transport)
    for value in (max_objects, max_pages, max_bytes):
        if type(value) is not int or value <= 0:
            raise ContractError('Discovery listing bounds must be positive integers')
    report = dict(uris=[], pages=0, objects=0, metadata_bytes=0)
    def check():
        if (report['objects'] > max_objects or report['pages'] > max_pages
                or report['metadata_bytes'] > max_bytes
                or deadline is not None and time.monotonic() >= deadline):
            raise ContractError('Discovery listing budget exhausted; no fresh fallback is permitted')
    if prefix.startswith('s3:'):
        remote = store if store.transport == 's3' else store._remote
        bucket, key = parse_s3(prefix, prefix=True)
        token = None
        seen = set()
        while True:
            check()
            if report['pages'] >= max_pages:
                raise ContractError('Discovery page budget exhausted; listing is incomplete')
            args = dict(Bucket=bucket, Prefix=key, MaxKeys=min(1000, max_objects + 1))
            if token is not None:
                args['ContinuationToken'] = token
            page = remote.policy.call('discover_list_page', lambda: remote.client.list_objects_v2(**args))
            report['pages'] += 1
            entries = page.get('Contents', [])
            if not isinstance(entries, list):
                raise IntegrityError('Malformed discovery listing')
            def encode(value):
                if isinstance(value, datetime):
                    return value.isoformat()
                raise ContractError('Unsupported discovery listing metadata')
            report['metadata_bytes'] += len(json.dumps(entries, default=encode, ensure_ascii=True,
                separators=(',', ':'), allow_nan=False).encode('utf-8'))
            for entry in entries:
                name = entry.get('Key') if isinstance(entry, dict) else None
                if not isinstance(name, str) or not name.startswith(key) or name in seen:
                    raise IntegrityError('Discovery listing escaped scope or repeated an object')
                seen.add(name)
                report['objects'] += 1
                check()
                # S3 directory markers are entries, but not scientific objects.
                if not name.endswith('/'):
                    report['uris'].append('s3://' + bucket + '/' + name)
            check()
            if not page.get('IsTruncated', False):
                break
            next_token = page.get('NextContinuationToken')
            if not isinstance(next_token, str) or not next_token or next_token == token:
                raise IntegrityError('Incomplete discovery listing has no advancing continuation')
            token = next_token
    else:
        root = parse_file(prefix + 'discovery-scope').parent
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        def walk(descriptor, path, depth):
            if depth > 32:
                raise ContractError('Discovery directory depth exceeds its bound')
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    report['objects'] += 1
                    report['metadata_bytes'] += len(entry.name.encode('utf-8'))
                    check()
                    target = path / entry.name
                    if entry.is_symlink():
                        raise IntegrityError('Discovery does not follow local symlinks')
                    if entry.is_dir(follow_symlinks=False):
                        child = os.open(entry.name, flags, dir_fd=descriptor)
                        try:
                            walk(child, target, depth + 1)
                        finally:
                            os.close(child)
                    elif entry.is_file(follow_symlinks=False):
                        report['uris'].append(target.as_uri())
                    else:
                        raise IntegrityError('Discovery requires regular local objects')
        try:
            with store._parent(prefix + 'discovery-scope') as (descriptor, _):
                report['pages'] = 1
                walk(descriptor, root, 0)
        except ObjectMissing:
            pass
    check()
    report['uris'].sort(reverse=True)
    return report
