#!/usr/bin/env python3
"""Prepare locked public artifacts, then build the offline NWB I/O candidate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True, help='Fresh build/evidence directory outside Git')
    parser.add_argument('--output', type=Path, required=True, help='New image-record JSON outside Git')
    args = parser.parse_args()
    if args.work_dir.exists() or args.output.exists():
        parser.error('Choose fresh work and output paths.')
    context = args.work_dir.resolve()
    context.mkdir(parents=True)
    lock = json.loads((HERE/'artifacts.lock.json').read_text())
    parent = lock['parent_image']
    prepare = ['docker', 'run', '--rm', '--user', str(os.getuid())+':'+str(os.getgid()),
               '-e', 'HOME=/tmp', '-v', str(HERE)+':/recipe:ro', '-v', str(context)+':/build',
               '--entrypoint', 'python', parent, '/recipe/prepare.py']
    subprocess.run(prepare, check=True)
    reference = 'braingeneers/mea-ephys-nwb:'+lock['image_version']
    command = ['docker', 'build', '--network', 'none', '--pull=false',
               '--file', str(HERE/'Dockerfile'), '--tag', reference, str(context)]
    subprocess.run(command, check=True)
    observed = json.loads(subprocess.check_output(['docker', 'image', 'inspect', reference]))[0]
    report = dict(status='locally_built_nwb_io_candidate_not_activated_or_published', reference=reference,
                  local_image_id=observed['Id'], registry_digest=None, parent=parent,
                  artifacts_lock_sha256=hashlib.sha256((HERE/'artifacts.lock.json').read_bytes()).hexdigest(),
                  dockerfile_sha256=hashlib.sha256((HERE/'Dockerfile').read_bytes()).hexdigest(),
                  preparation_command=prepare, build_command=command,
                  preparation=json.loads((context/'preparation.json').read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
