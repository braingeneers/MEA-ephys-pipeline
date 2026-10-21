#!/usr/bin/env python3
"""Compare the maintained Nextflow code digest with an independent byte oracle.

Run inside the pinned Nextflow driver. No tasks or scientific capsules execute.
The caller may select the preloaded older engine with --executable.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def oracle(inputs):
    digest = hashlib.sha256(b'mea.staged-code/v1\0')

    def visit(path, relative):
        if path.is_dir():
            digest.update(b'D\0' + relative.encode() + b'\0')
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child, relative + '/' + child.name)
        else:
            content = path.read_bytes()
            digest.update(b'F\0' + relative.encode() + b'\0' + str(len(content)).encode() + b'\0')
            digest.update(content)

    for index, path in enumerate(inputs):
        visit(path, str(index))
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', default='nextflow')
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[2]
    base = root / 'base'
    (base / 'nested').mkdir(parents=True)
    (base / 'nested/a.py').write_bytes(b'value = 1\n')
    (base / 'nested/b.py').write_bytes(b'value = 2\n')
    (base / 'empty').mkdir()
    cases = [('base', [base])]
    for name in ['relocated', 'same-size-edit', 'renamed', 'swapped', 'added', 'removed']:
        folder = root / name
        shutil.copytree(base, folder)
        cases.append((name, [folder]))
    # Change bytes while preserving size, file mtime, and directory metadata.
    edited = root / 'same-size-edit/nested/a.py'
    stat = edited.stat()
    edited.write_bytes(b'value = 9\n')
    os.utime(edited, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    (root / 'renamed/nested/a.py').rename(root / 'renamed/nested/c.py')
    (root / 'swapped/nested/a.py').write_bytes(b'value = 2\n')
    (root / 'swapped/nested/b.py').write_bytes(b'value = 1\n')
    (root / 'added/nested/new.py').write_bytes(b'new = True\n')
    # Retain the removed file outside the candidate rather than deleting it.
    (root / 'removed/nested/a.py').rename(root / 'removed-retained.py')
    linked = root / 'linked'
    linked.symlink_to(base, target_is_directory=True)
    single = base / 'nested/a.py'
    second = base / 'nested/b.py'
    cases += [('linked-root', [linked]), ('single', [single]),
              ('ordered', [single, second]), ('reordered', [second, single])]
    cycle = root / 'cycle'
    cycle.mkdir()
    (cycle / 'back').symlink_to(cycle, target_is_directory=True)
    request = [dict(name=name, inputs=[str(path) for path in inputs]) for name, inputs in cases]
    request += [dict(name='cycle', inputs=[str(cycle)]),
                dict(name='missing', inputs=[str(root / 'absent')])]
    (root / 'request.json').write_text(json.dumps(request))
    command = [args.executable, '-log', str(root / 'nextflow.log'), 'run',
               str(source / 'scripts/modular/check_code_identity.nf'),
               '--request', str(root / 'request.json'), '--output', str(root / 'actual.json'),
               '-with-trace', str(root / 'trace.tsv'), '-ansi-log', 'false']
    (root / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    with (root / 'run.log').open('w') as log:
        result = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT, timeout=180)
    assert result.returncode == 0, (root / 'run.log').read_text()
    actual = {row['name']: row for row in json.loads((root / 'actual.json').read_text())}
    expected = {name: oracle(inputs) for name, inputs in cases}
    for name, value in expected.items():
        assert actual[name]['digest'] == value, (name, actual[name], value)
    for name, inputs in cases:
        assert actual[name]['port_digest'] == oracle(inputs[:1])
        assert actual[name]['port_path'] == str(inputs[0])
    assert expected['base'] == expected['relocated'] == expected['linked-root']
    assert all(expected[name] != expected['base'] for name in ['same-size-edit', 'renamed', 'swapped', 'added', 'removed'])
    assert expected['ordered'] != expected['reordered']
    assert 'Cyclic staged code directory' in actual['cycle']['message']
    assert 'error' in actual['missing']
    assert len((root / 'trace.tsv').read_text().splitlines()) == 1
    report = dict(passed=True, source=str(source), checks=len(request), expected=expected,
                  actual=actual, independent_byte_oracle=True, scientific_tasks=0,
                  same_size_preserved_mtime_edit_detected=True)
    (root / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print('PASS {} code identity cases; no tasks'.format(len(request)), flush=True)


if __name__ == '__main__':
    main()
