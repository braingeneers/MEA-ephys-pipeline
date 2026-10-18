"""Apply narrowly pinned NWB backend compatibility fixes at image build time."""
import base64
import csv
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import py_compile


BEFORE = b'    manager = get_manager()\n    builder = manager.build(nwbfile)\n'
AFTER = (
    b"    # Loaded containers belong to their source reader's invocation type map.\n"
    b'    # Preserve that manager; a foreign global map can omit cached-schema fields.\n'
    b'    source_io = nwbfile.read_io\n'
    b'    manager = source_io.manager if source_io is not None else get_manager()\n'
    b'    builder = manager.build(nwbfile)\n'
)
CACHE_BEFORE = b'            self.__cache_spec()\n\n    def get_written('
CACHE_AFTER = (
    b'            self.__cache_spec()\n'
    b'            # Specifications are written after write_builder consolidates metadata.\n'
    b'            if write_args.get("consolidate_metadata", True):\n'
    b'                zarr.consolidate_metadata(store=self.path)\n\n'
    b'    def get_written('
)


def rewrite(source, version, lock, before=BEFORE, after=AFTER):
    if version != lock['version']:
        raise ValueError('Unexpected dependency version; inspect before adapting it.')
    if hashlib.sha256(source).hexdigest() != lock['source_sha256'] or source.count(before) != 1:
        raise ValueError('Unexpected dependency source; refusing an unverified patch.')
    changed = source.replace(before, after)
    if hashlib.sha256(changed).hexdigest() != lock['patched_sha256']:
        raise ValueError('Patched dependency content differs from the reviewed lock.')
    return changed


def patch(lock, before, after):
    distribution = importlib.metadata.distribution(lock['distribution'])
    module = Path(distribution.locate_file(lock['module']))
    changed = rewrite(module.read_bytes(), distribution.version, lock, before, after)
    record_name = next(str(p) for p in distribution.files if str(p).endswith('.dist-info/RECORD'))
    record = Path(distribution.locate_file(record_name))
    original_record = record.read_bytes()
    rows = list(csv.reader(io.StringIO(original_record.decode())))
    matches = [row for row in rows if row[0] == lock['module']]
    if len(matches) != 1:
        raise ValueError('Dependency RECORD does not uniquely identify the patched module.')
    matches[0][1:] = [
        'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(changed).digest()).decode().rstrip('='),
        str(len(changed)),
    ]
    serialized = io.StringIO(newline='')
    csv.writer(serialized).writerows(rows)
    module.write_bytes(changed)
    record.write_bytes(serialized.getvalue().encode())
    py_compile.compile(str(module), doraise=True)
    return dict(lock, source_file=str(module), record_file=record_name,
                record_before_sha256=hashlib.sha256(original_record).hexdigest(),
                record_after_sha256=hashlib.sha256(record.read_bytes()).hexdigest())


def main():
    root = Path(__file__).resolve().parent
    lock = json.loads((root / 'patch.lock.json').read_text())
    if lock['schema_version'] != 2 or len(lock['patches']) != 2:
        raise ValueError('Unexpected patch recipe.')
    reports = [patch(lock['patches'][0], BEFORE, AFTER),
               patch(lock['patches'][1], CACHE_BEFORE, CACHE_AFTER)]
    report = dict(schema_version=2, patches=reports, package_versions_changed=False)
    (root / 'patch-result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
