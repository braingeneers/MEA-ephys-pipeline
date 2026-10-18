"""Prepare checksum-locked I/O wheels in a fresh build context, inside Docker."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
OUTPUT = Path('/build')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(record, destination):
    with urlopen(record['url'], timeout=60) as response:
        payload = response.read()
    if hashlib.sha256(payload).hexdigest() != record['sha256']:
        raise ValueError('Artifact checksum mismatch: '+record['filename'])
    if 'bytes' in record and len(payload) != record['bytes']:
        raise ValueError('Artifact size mismatch: '+record['filename'])
    destination.write_bytes(payload)


def main():
    lock = json.loads((HERE/'artifacts.lock.json').read_text())
    wheels = OUTPUT/'wheelhouse'
    wheels.mkdir()
    build_wheels = OUTPUT/'build-wheels'
    build_wheels.mkdir()
    for record in lock['runtime']:
        if not record.get('from_source'):
            download(record, wheels/record['filename'])
    for record in lock['build_tools']:
        download(record, build_wheels/record['filename'])
    tools_lock = OUTPUT/'build-tools.lock'
    tools_lock.write_text(''.join(record['name']+'=='+record['version']+' --hash=sha256:'+record['sha256']+'\n'
                                  for record in lock['build_tools']))
    tool_directory = OUTPUT/'build-tools'
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', '--no-index',
                    '--find-links', str(build_wheels), '--no-deps', '--require-hashes',
                    '--target', str(tool_directory), '-r', str(tools_lock)], check=True)
    source = lock['ndx_source']
    archive = OUTPUT/source['filename']
    download(source, archive)
    env = os.environ.copy()
    env.update(PYTHONPATH=str(tool_directory), SOURCE_DATE_EPOCH=str(lock['source_date_epoch']))
    subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                    '--wheel-dir', str(wheels), str(archive)], env=env, check=True)
    for record in lock['runtime']:
        path = wheels/record['filename']
        if not path.is_file() or digest(path) != record['sha256'] or path.stat().st_size != record['bytes']:
            raise ValueError('Prepared wheel differs from locked artifact: '+record['filename'])
    for name in ('requirements.lock', 'artifacts.lock.json'):
        shutil.copyfile(HERE/name, OUTPUT/name)
    report = dict(passed=True, runtime_files={record['filename']:digest(wheels/record['filename'])
                                           for record in lock['runtime']},
                  ndx_source_commit=source['commit'], build_tools_outside_runtime=True)
    (OUTPUT/'preparation.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
