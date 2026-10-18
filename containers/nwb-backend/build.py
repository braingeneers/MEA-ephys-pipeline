#!/usr/bin/env python3
"""Build a local inactive backend candidate from the exact accepted NWB parent."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent


def inspect(reference):
    return json.loads(subprocess.check_output(['docker', 'image', 'inspect', reference]))[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Fresh build record outside Git')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Choose a fresh output path.')
    lock = json.loads((HERE / 'patch.lock.json').read_text())
    parent = inspect(lock['parent_image_id'])
    if parent['Config']['User'] != 'pipelineuser' or parent['Os'] != 'linux' or parent['Architecture'] != 'amd64':
        parser.error('Unexpected parent runtime contract.')
    alias = 'braingeneers/mea-build-parent:sha256-' + parent['Id'].split(':', 1)[1]
    subprocess.run(['docker', 'tag', parent['Id'], alias], check=True)
    reference = 'braingeneers/mea-ephys-nwb:' + lock['image_version']
    command = ['docker', 'build', '--network', 'none', '--pull=false',
               '--build-arg', 'PARENT_IMAGE=' + alias, '--tag', reference, str(HERE)]
    subprocess.run(command, check=True)
    current = inspect(reference)
    layers = parent['RootFS']['Layers']
    if current['RootFS']['Layers'][:len(layers)] != layers or inspect(alias)['Id'] != parent['Id']:
        raise RuntimeError('Candidate does not retain its exact scientific parent.')
    if current['Config']['User'] != parent['Config']['User'] or current['Config']['Env'] != parent['Config']['Env']:
        raise RuntimeError('Candidate changed inherited user/environment settings.')
    report = dict(schema_version=1, status='locally_built_backend_candidate_not_activated_or_published',
                  reference=reference, local_image_id=current['Id'], registry_digest=None,
                  parent=parent['Id'], parent_reference=lock['parent_reference'], command=command,
                  patch=lock, source_sha256={name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
                                            for name in ('Dockerfile', 'patch.lock.json', 'patch_runtime.py')})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
