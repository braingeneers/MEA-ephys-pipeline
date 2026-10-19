"""Export the maintained fresh Maxwell component from an exact Git commit.

Python 3.11+ and Git are the only generator dependencies. Scientific dependencies
remain in the pinned owning image; this command never clones or installs code.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys

OWNER = 'workflow-sources/modular-maxwell-to-nwb'
COMMON = 'workflow-sources/ephys-modular-common'
GENERATOR = OWNER + '/export_component.py'
MANIFEST = 'export-manifest.json'
FORMAT = 1


class ExportError(ValueError):
    pass


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(value):
    if (not isinstance(value, str) or not value or '\\' in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value) or value.startswith('/')):
        raise ExportError('Expected a safe relative source path')
    if any(p in ('', '.', '..') for p in value.split('/')):
        raise ExportError('Source paths cannot contain empty or parent components')
    return PurePosixPath(value)


class GitSource:
    def __init__(self, directory, revision):
        if not re.fullmatch('[a-f0-9]{40}', revision):
            raise ExportError('source-revision must be a full lowercase Git commit SHA')
        self.directory, self.revision = Path(directory).absolute(), revision
        observed = self.git('rev-parse', '--verify', revision + '^{commit}').decode().strip()
        if observed != revision:
            raise ExportError('Source revision is not the requested immutable commit')
        self.entries = {}
        for row in self.git('ls-tree', '-rz', revision).split(b'\0'):
            if not row:
                continue
            details, path = row.split(b'\t', 1)
            mode, kind, identity = details.decode().split()
            name = path.decode('utf-8')
            self.entries[name] = (mode, kind, identity)

    def git(self, *arguments):
        result = subprocess.run(['git', '-C', str(self.directory), *arguments],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise ExportError('Git could not read the exact source commit')
        return result.stdout

    def read(self, path):
        safe_path(path)
        entry = self.entries.get(path)
        if entry is None or entry[0] not in ('100644', '100755') or entry[1] != 'blob':
            raise ExportError('Required source must be a regular committed file: ' + path)
        return self.git('cat-file', 'blob', entry[2])


def imports(name, data):
    found = set()
    for node in ast.walk(ast.parse(data)):
        if isinstance(node, ast.Import):
            found.update(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level:
                parts = name.split('.')[:-node.level]
                found.add('.'.join([*parts, node.module]))
            else:
                found.add(node.module)
    return found


def selection(source):
    """Static fresh imports plus explicit subprocess/identity/data-file roots."""
    paths = source.entries
    extraction = json.loads(source.read(OWNER + '/extraction.json'))
    science = {**extraction['scripts'], **extraction.get('owned_helpers', {})}
    for name, record in science.items():
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*\.py', name):
            raise ExportError('Unsupported scientific extraction filename')
        if checksum(source.read(OWNER + '/scripts/' + name)) != record['extracted_sha256']:
            raise ExportError('Scientific source differs from its extraction record: ' + name)
    modules = {}
    for path in paths:
        parent = str(PurePosixPath(path).parent)
        if path.endswith('.py') and parent in (OWNER + '/runtime', OWNER + '/scripts'):
            modules[PurePosixPath(path).stem] = path
        elif path.startswith(COMMON + '/python/ephys_modular/') and path.endswith('.py'):
            relative = path[len(COMMON + '/python/'): -3].replace('/', '.')
            modules[relative.removesuffix('.__init__')] = path
    pending, reachable = ['native_task', 'verify_outputs'], set()
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        if name not in modules:
            raise ExportError('Required runtime root is missing: ' + name)
        if name.startswith(('adoption_', 'reuse_')):
            raise ExportError('Fresh runtime now requires adoption/reuse; review the export boundary')
        reachable.add(name)
        dependencies = imports(name, source.read(modules[name]))
        for item in dependencies:
            if item.startswith('ephys_modular.') and item not in modules:
                raise ExportError('Required common module is missing: ' + item)
        pending.extend(item for item in dependencies if item in modules)
    selected = {}
    for name in reachable:
        if modules[name].startswith(OWNER + '/runtime/'):
            path = modules[name]
            selected[path] = (path[len(OWNER) + 1:], 'task-runtime')
    for name in science:
        selected[OWNER + '/scripts/' + name] = ('scripts/' + name, 'scientific-image-source')
    fixed = ('main.nf', 'component.nf', 'nextflow.config', 'Dockerfile', 'defaults.json',
             'image.json', 'process-policy.json', 'runtime-lock.json', 'extraction.json',
             'runtime/allocation-policy.json', 'runtime/owner-runtime-policy.json',
             'lib/MaxwellTaskRetry.groovy', 'export_component.py')
    for name in fixed:
        role = ('distribution' if name in ('Dockerfile', 'export_component.py') else
                'scientific-image-metadata' if name in ('defaults.json', 'extraction.json', 'runtime-lock.json')
                else 'task-runtime')
        selected[OWNER + '/' + name] = (name, role)
    selected[COMMON + '/runtime.config'] = ('components/common/runtime.config', 'deployment')
    for path in paths:
        if path.startswith(COMMON + '/python/') and path.endswith(('.py', '.json')):
            selected[path] = ('components/common/python/' + path[len(COMMON + '/python/'):], 'task-runtime')
    notices = []
    scopes = {'.', 'workflow-sources', OWNER, COMMON}
    for path in paths:
        if (str(PurePosixPath(path).parent) in scopes
                and PurePosixPath(path).name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE'))):
            selected[path] = ('notices/' + path, 'upstream-notice')
            notices.append(path)
    return selected, sorted(notices)


def portable_readme(revision, version, image):
    return f'''# Generated Maxwell conversion component

Generated from https://github.com/braingeneers/workflows at `{revision}`.
Owning workflow version: `{version}`. Scientific task image: `{image}`.
Generated scientific/controller files retain their upstream bytes and headers.
Only Docker COPY source paths are relocated. No license is inferred or reassigned;
consult the recorded upstream notices/headers and source provenance.

This is the complete fresh conversion component: source validation, conversion,
channel-major repack, compact companion, LINDI, final validation and verified
publication. Source replacement/finalization is not supported. The native artifact
contract is preserved. An MEA artifact-to-local-unit bridge is a separate caller.

Requires Nextflow 26.04.6 and Docker for local tasks. Its config includes the pinned
nf-k8s deployment plugin; driver plugin acquisition is distinct from scientific
tasks, which require no package or Maxwell plugin downloads. Keep this config
separate from older KS4/NERSC entry profiles. The root lib directory supplies
MaxwellTaskRetry; foreign imports must explicitly arrange its classpath.

Run from a separate launch directory, using absolute paths for all local inputs
and outputs. A standalone local `params.json` can contain:

```json
{{"recordings":[{{"input_uri":"file:///data/recording.raw.h5","data_format":"maxone"}}],
 "output_uri":"file:///data/results/","collection_uri":"file:///data/results/derived/nwb/",
 "settings":{{}}}}
```

```bash
nextflow run /path/to/component/main.nf -profile docker -params-file /path/to/params.json
python3.11 /path/to/component/export_component.py verify /path/to/component
```

Local file transport is explicit and standalone-only. S3 callers provide complete
recordings or verified artifact references, explicit output/collection/cache roots
where needed, a public reader endpoint and normal credentials. The workflow owns
transfers/verification and emits only after publication. Do not mix full runtime
configs into other workflows. See main.nf/component.nf for the exact native ports.

For exact JSON numeric fidelity, pass recordings/settings as JSON strings or call
the exported maxwellJson(text) before creating value channels. Its pinned-runtime
Jackson decoder retains arbitrary-precision integers and finite binary64 floats,
including negative zero. It rejects duplicate keys, trailing content and non-finite
values. Nextflow's own params-file object reader and Groovy JsonSlurper can alter
numbers before this component receives them; typed inputs retain their supplied
values, not a recoverable copy of the original bytes.

The complete tree includes image build source as well as staged runtime. Consumers
may select a smaller explicit runtime closure; do not ship tests/scientific data or
discard common schemas/libraries. export-manifest.json records roles and hashes.

Regenerate from the exact committed exporter, with Python 3.11+ and Git:

```bash
python3.11 export_component.py export --source-repository /path/to/workflows \\
  --source-revision {revision} --output /path/to/new-component
```

For a local development rebuild (not a published image or replacement release pin):

```bash
docker build --network none --build-arg SOURCE_REVISION={revision} \\
  -t maxwell-export-check:development /path/to/component
docker run --rm --network none --entrypoint python maxwell-export-check:development \\
  scripts/runtime_identity.py
```

The pinned base image must already be available. Scientific dependencies, plugin
bytes and extraction/runtime locks remain unchanged. Rebuilt image identity can
differ from the published task image; record that development identity explicitly.
'''.encode()


def export_component(repository, revision, output):
    source = GitSource(repository, revision)
    if source.read(GENERATOR) != Path(__file__).read_bytes():
        raise ExportError('Run the exporter from the selected source revision')
    selected, notices = selection(source)
    image = json.loads(source.read(OWNER + '/image.json'))
    if (not re.fullmatch(r'[^\s]+@sha256:[a-f0-9]{64}', image['pinned_reference'])
            or image['pinned_reference'].rsplit('@', 1)[1] != image['registry_digest']):
        raise ExportError('Scientific image requires a consistent immutable pin')
    config = source.read(OWNER + '/nextflow.config').decode()
    versions = re.findall(r"(?m)^\s*version\s*=\s*['\"]([^'\"]+)['\"]", config)
    if len(versions) != 1:
        raise ExportError('Expected one literal owning workflow version')
    records, contents = {}, {}
    for upstream, (relative, role) in sorted(selected.items()):
        safe_path(relative)
        data = original = source.read(upstream)
        transform = 'copy'
        if relative == 'Dockerfile':
            text = data.decode()
            prefix = OWNER + '/'
            lines = re.findall(r'(?m)^COPY ' + re.escape(prefix), text)
            if len(lines) != 5:
                raise ExportError('Docker build context changed; review its source relocation')
            data = re.sub(r'(?m)^COPY ' + re.escape(prefix), 'COPY ', text).encode()
            transform = 'relocate-five-Docker-COPY-prefixes'
        if relative.endswith('.py'):
            compile(data, relative, 'exec')
        if relative in records:
            raise ExportError('Export destination collision')
        records[relative] = dict(source_path=upstream, source_sha256=checksum(original),
            sha256=checksum(data), bytes=len(data), mode=source.entries[upstream][0],
            role=role, transform=transform)
        contents[relative] = data
    readme = portable_readme(revision, versions[0], image['pinned_reference'])
    records['README.md'] = dict(source_path=None, source_sha256=None, sha256=checksum(readme),
        bytes=len(readme), mode='100644', role='distribution', transform='generated-portable-instructions')
    contents['README.md'] = readme
    manifest = dict(schema_version=FORMAT, source_repository='https://github.com/braingeneers/workflows',
        source_commit=revision, owner_version=versions[0], scientific_image=image,
        generator=dict(path=GENERATOR, sha256=checksum(source.read(GENERATOR))),
        applicable_notice_files=notices, files=records,
        regeneration=['python3.11', 'export_component.py', 'export', '--source-repository',
            '/path/to/workflows', '--source-revision', revision, '--output', '/path/to/new-component'],
        scientific_source_transformed=False, source_replacement_supported=False)
    manifest['content_sha256'] = checksum(encoded(manifest))
    output = Path(output).absolute()
    top = Path(source.git('rev-parse', '--show-toplevel').decode().strip()).resolve()
    if output.resolve().is_relative_to(top):
        raise ExportError('Prepare the export outside its source repository')
    if output.exists() or output.is_symlink():
        raise ExportError('Output must be a new directory; existing exports are never replaced')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()
    for relative, data in contents.items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(data)
        target.chmod(0o755 if records[relative]['mode'] == '100755' else 0o644)
    with (output / MANIFEST).open('xb') as stream:
        stream.write(encoded(manifest))
    (output / MANIFEST).chmod(0o644)
    return verify_export(output)


def verify_export(directory):
    root = Path(directory).absolute()
    if root.is_symlink() or not root.is_dir() or (root / MANIFEST).is_symlink():
        raise ExportError('Export root and manifest must be ordinary local files/directories')
    manifest = json.loads((root / MANIFEST).read_bytes())
    if stat.S_IMODE((root / MANIFEST).stat().st_mode) != 0o644:
        raise ExportError('Export manifest mode changed')
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
        raise ExportError('Export manifest must contain an object file inventory')
    document = dict(manifest)
    claimed = document.pop('content_sha256', None)
    if manifest.get('schema_version') != FORMAT or checksum(encoded(document)) != claimed:
        raise ExportError('Invalid or changed export manifest')
    expected = set(manifest['files']) | {MANIFEST}
    actual = set()
    for base, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(base) / name
            if path.is_symlink():
                raise ExportError('Export contains a symlink')
            if name in files:
                if not stat.S_ISREG(path.stat().st_mode):
                    raise ExportError('Export contains a non-regular file')
                actual.add(path.relative_to(root).as_posix())
    if actual != expected:
        raise ExportError('Export file inventory differs from its manifest')
    for name, item in manifest['files'].items():
        safe_path(name)
        if item['mode'] not in ('100644', '100755'):
            raise ExportError('Unsupported export file mode')
        path = root / name
        data = path.read_bytes()
        if len(data) != item['bytes'] or checksum(data) != item['sha256']:
            raise ExportError('Export file changed or incomplete: ' + name)
        if stat.S_IMODE(path.stat().st_mode) != (0o755 if item['mode'] == '100755' else 0o644):
            raise ExportError('Export file mode changed: ' + name)
    return manifest


def main():
    if sys.version_info < (3, 11):
        raise ExportError('The source generator requires Python 3.11 or newer')
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    create = commands.add_parser('export')
    create.add_argument('--source-repository', required=True)
    create.add_argument('--source-revision', required=True)
    create.add_argument('--output', required=True)
    check = commands.add_parser('verify')
    check.add_argument('directory')
    args = parser.parse_args()
    result = (verify_export(args.directory) if args.operation == 'verify' else
              export_component(args.source_repository, args.source_revision, args.output))
    print(json.dumps(dict(source_commit=result['source_commit'], files=len(result['files']),
                          content_sha256=result['content_sha256']), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, SyntaxError) as error:
        print('[export_component] ' + str(error), file=sys.stderr)
        raise SystemExit(2) from None
