"""Explicit producer locations captured before scratch output staging.

These are source-only instructions. Published executable descriptors contain only
relative paths; the original record remains immutable historical provenance.
"""
import hashlib
import json
from pathlib import Path
import re

PREFIX = '.mea-producer-location-'
ROLES = {'job_dispatch', 'preprocessing', 'spikesort_kilosort4',
         'postprocessing', 'curation', 'results_collector'}
MAX_BYTES = 8 * 1024 * 1024


class LocationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise LocationError(message)


def absolute(value):
    require(isinstance(value, str) and value and Path(value).is_absolute(),
            'Producer location requires absolute paths')
    path = Path(value)
    require(str(path) == value and '..' not in path.parts,
            'Producer location path is not canonical')
    return path


def validate(record):
    require(isinstance(record, dict) and set(record) == {
        'schema_version', 'document_type', 'role', 'recorder_sha256',
        'producer_root', 'published_root', 'published_results', 'links'}, 'Malformed producer location record')
    require(type(record['schema_version']) is int and record['schema_version'] == 1
            and record['document_type'] == 'mea_producer_location', 'Unsupported producer location record')
    require(isinstance(record['role'], str) and record['role'] in ROLES, 'Unknown producer location role')
    require(isinstance(record['recorder_sha256'], str)
            and re.fullmatch('[0-9a-f]{64}', record['recorder_sha256']), 'Invalid producer recorder identity')
    producer, published = absolute(record['producer_root']), absolute(record['published_root'])
    results = absolute(record['published_results'])
    require(producer.name == published.name == 'capsule'
            and results == published.parent / 'unit-results',
            'Producer publication differs from the declared module output ports')
    require(results.resolve().is_relative_to(published.parent),
            'Producer publication escapes its shared task directory')
    require(isinstance(record['links'], list), 'Producer links must be an array')
    seen = set()
    for link in record['links']:
        require(isinstance(link, dict) and set(link) == {'source', 'target'}, 'Malformed producer link')
        source, target = absolute(link['source']), absolute(link['target'])
        require(any(source.is_relative_to(producer / name) for name in ('data', 'results')),
                'Producer link escapes its declared input/output roots')
        require(source not in seen, 'Duplicate or ambiguous producer link')
        seen.add(source)
    return record


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate producer location JSON field')
            result[key] = value
        return result
    return validate(json.loads(raw, object_pairs_hook=pairs))


def load(source, expected_identity=None):
    """Find this exact declared results producer, without deriving an origin."""
    source = Path(source).resolve(strict=True)
    matches = []
    for path in sorted(source.glob(PREFIX + '*.json')):
        require(path.is_file() and not path.is_symlink(), 'Producer location must be an owned regular file')
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES, 'Producer location record is oversized')
        record = decode(raw)
        identity = hashlib.sha256((record['published_root'] + '\0' + record['role']).encode()).hexdigest()[:24]
        require(path.name == PREFIX + identity + '.json', 'Producer location filename differs from its binding')
        if Path(record['published_results']).resolve() == source:
            if expected_identity is not None:
                require(record['recorder_sha256'] == expected_identity, 'Producer recorder code identity differs')
            matches.append(dict(record=record, source=str(path), sha256=hashlib.sha256(raw).hexdigest()))
    require(len(matches) <= 1, 'Ambiguous current producer location records')
    require(matches or expected_identity is None, 'Missing required producer location record')
    return matches[0] if matches else None


def old_base(base, provenance):
    """Map a staged base back to its explicitly captured original coordinate."""
    record = validate(provenance['record'])
    base = Path(base).resolve()
    published = Path(record['published_root'])
    results = Path(record['published_results']).resolve()
    if base.is_relative_to(results):
        return Path(record['producer_root']) / 'results' / base.relative_to(results)
    if base.is_relative_to(published):
        return Path(record['producer_root']) / base.relative_to(published)
    # The collector can deliberately serialize against its publication base.
    # That base is already explicit and must not be replaced by a task directory.
    return base


def resolve(path, provenance):
    """Resolve old symlinks before '..', then map owned output paths to staging."""
    record = validate(provenance['record'])
    path = Path(path)
    require(path.is_absolute(), 'Producer dependency lacks an absolute source coordinate')
    producer, published = Path(record['producer_root']), Path(record['published_root'])
    links = {Path(row['source']): Path(row['target']) for row in record['links']}
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if part == '..':
            current = current.parent
        elif part != '.':
            current = current / part
            if current in links:
                current = links[current]
            elif not current.is_relative_to(producer) and not producer.is_relative_to(current):
                # Stable dependencies outside retired scratch still use their
                # actual live filesystem links. Never inspect a reused old root.
                current = current.resolve()
    if current.is_relative_to(producer / 'results'):
        current = Path(record['published_results']) / current.relative_to(producer / 'results')
    elif current.is_relative_to(producer):
        current = published / current.relative_to(producer)
    return current.resolve(strict=True)
