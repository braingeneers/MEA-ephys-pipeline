#!/usr/bin/env python3
"""Build candidate codec images from immutable scientific parent identities."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PARENTS = {
    'base': 'ghcr.io/allenneuraldynamics/aind-ephys-pipeline-base@sha256:a9728fa63078b0429e4c85b5918d7286d4c3f6ed5def900f9876540513adec03',
    'ks4': 'ghcr.io/allenneuraldynamics/aind-ephys-spikesort-kilosort4@sha256:b5192e8b2b4f62f07b98ed693f23a192dff04c119e744946ac000589026b8165',
    'nwb': 'ghcr.io/allenneuraldynamics/aind-ephys-pipeline-nwb@sha256:03ad8e8572ad1e98da9d35c380e8fad23de895731d2d382fd9e18376dd61ab4b',
}
REFERENCES = {
    'base': 'braingeneers/mea-ephys-base:0.1.0',
    'ks4': 'braingeneers/mea-ephys-kilosort4:0.1.0',
    'analysis': 'braingeneers/mea-ephys-analysis:0.2.0',
    'nwb': 'braingeneers/mea-ephys-nwb:0.1.0',
}


def inspect(reference):
    return json.loads(subprocess.check_output(['docker', 'image', 'inspect', reference]))[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New build-record JSON outside the source tree')
    parser.add_argument('--roles', nargs='+', choices=REFERENCES, default=list(REFERENCES))
    parser.add_argument('--report-parent', help='Immutable ID/digest of the independently built original report image')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Build record already exists; choose a new output file.')
    parents = dict(PARENTS)
    old_report = json.loads((ROOT/'containers/analysis/image.json').read_text())
    parents['analysis'] = args.report_parent or old_report['local_image_id']
    records = {}
    for role in dict.fromkeys(args.roles):
        parent = parents[role]
        if not re.fullmatch(r'(?:[A-Za-z0-9._:/-]+@)?sha256:[0-9a-f]{64}', parent):
            parser.error('Every parent must be an immutable Docker ID or registry digest.')
        original = inspect(parent)
        if original['Os'] != 'linux' or original['Architecture'] != 'amd64':
            parser.error('The pinned Maxwell codec requires the verified linux/amd64 images.')
        if original['Config'].get('User') != 'pipelineuser':
            parser.error('The recipe preserves the known pipelineuser parent contract.')
        if any(value.startswith('HDF5_PLUGIN_PATH=') for value in original['Config'].get('Env', [])):
            parser.error('Parent already configures HDF5 plugins; inspect before replacing its path.')
        build_parent = parent
        if parent.startswith('sha256:'):
            # BuildKit resolves FROM through named image references. A raw config
            # ID is parsed as docker.io/library/sha256:<id>, even when cached.
            build_parent = 'braingeneers/mea-build-parent:sha256-'+parent.split(':', 1)[1]
            subprocess.run(['docker', 'tag', parent, build_parent], check=True)
            if inspect(build_parent)['Id'] != original['Id']:
                raise RuntimeError('Local parent build alias changed identity.')
        command = ['docker', 'build', '--network', 'none', '--pull=false',
                   '--file', str(HERE/'Dockerfile'), '--build-arg', 'PARENT_IMAGE='+build_parent,
                   '--tag', REFERENCES[role], str(HERE)]
        subprocess.run(command, check=True)
        current = inspect(REFERENCES[role])
        layers = original['RootFS']['Layers']
        if current['RootFS']['Layers'][:len(layers)] != layers or inspect(build_parent)['Id'] != original['Id']:
            raise RuntimeError('Built image does not retain its exact scientific parent.')
        records[role] = dict(parent=parent, parent_image_id=original['Id'],
                             build_parent_reference=build_parent,
                             reference=REFERENCES[role], local_image_id=current['Id'],
                             registry_digest=None, command=command)
    report = dict(schema_version=1, status='locally_built_candidates_not_activated_or_published',
                  dockerfile='containers/maxwell-codec/Dockerfile',
                  dockerfile_sha256=hashlib.sha256((HERE/'Dockerfile').read_bytes()).hexdigest(),
                  plugin_source_image='braingeneers/modular-maxwell-to-nwb@sha256:c3c204b9338470f5dba010f2214dc96c71e97841bcc36ebac4f950547f8f9eed',
                  plugin_sha256='4bc74ea98c08b70406aa976950fcf5f55d572430f51d3882eeb947c557dd14f4',
                  plugin_bytes=35776, report_parent_recipe='containers/analysis/Dockerfile',
                  report_parent_recipe_sha256=hashlib.sha256((ROOT/'containers/analysis/Dockerfile').read_bytes()).hexdigest(),
                  images=records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
