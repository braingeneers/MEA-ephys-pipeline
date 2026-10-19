#!/usr/bin/env python3
"""Full current converter oracle for the sealed 180-second boundary campaign.

Use a reviewed runtime-freeze.json plus its independently supplied SHA-256. The
manifest seals the completed source/input/work/publication/port directories. This
profile does not treat older executions as current producer evidence.
"""
import argparse
import ast
import importlib.metadata
import json
from pathlib import Path
import stat
from urllib.parse import unquote, urlsplit

import canonical
import current_graph
import storage
from canonical import require
from inputs import sha


class LongGraphScope:
    def __init__(self, root, trusted_sha256):
        self.root = root.resolve()
        require(sha(self.root / 'runtime-freeze.json') == trusted_sha256,
                'Trusted completed long-graph seal')
        self.freeze = json.loads((self.root / 'runtime-freeze.json').read_text())
        require(self.freeze['document_type'] == 'completed_long_graph_runtime_scope',
                'Explicit completed runtime seal format')
        actual_files, actual_links = set(), set()
        for name in self.freeze['selected_directories']:
            for path in (self.root / name).rglob('*'):
                relative = str(path.relative_to(self.root))
                if path.is_symlink():
                    require(relative in self.freeze['symlinks'] and
                            path.readlink().as_posix() == self.freeze['symlinks'][relative],
                            'Exact completed long-graph staging link ' + relative)
                    actual_links.add(relative)
                elif path.is_file():
                    self.frozen(path)
                    actual_files.add(relative)
        require(actual_files == set(self.freeze['files']) and
                actual_links == set(self.freeze['symlinks']),
                'Complete selected runtime scope inventory')
        for name in ('canonical.py', 'storage.py', 'inputs.py'):
            require(sha(Path(__file__).with_name(name)) ==
                    sha(self.root / 'source/tests/conversion_oracle' / name),
                    'Exact long-scope shared comparator ' + name)
        # The report oracle follows the exact declared projection path. All
        # report semantics and other artifact checks remain byte-identical.
        old_artifacts = (self.root / 'source/tests/conversion_oracle/artifacts.py').read_text()
        expected_artifacts = old_artifacts.replace(
            "projection = json.loads((folder / 'projection.json').read_text())",
            "projection = json.loads((folder / facts['projection']['file']).read_text())")
        require(Path(__file__).with_name('artifacts.py').read_text() == expected_artifacts,
                'Only declared projection-path lookup changes in artifact oracle')
        before = ast.parse((self.root / 'source/tests/conversion_oracle/current_graph.py').read_text())
        after = ast.parse(Path(current_graph.__file__).read_text())
        for name in ('observations', 'publication', 'current_sidecar_header', 'current_sidecar'):
            left, = [node for node in before.body if isinstance(node, ast.FunctionDef) and node.name == name]
            right, = [node for node in after.body if isinstance(node, ast.FunctionDef) and node.name == name]
            require(ast.dump(left) == ast.dump(right), 'Unchanged native scientific guard ' + name)
        versions = canonical.VERSIONS | storage.STORAGE_VERSIONS
        require({name: importlib.metadata.version(name) for name in versions} == versions,
                'Exact pinned independent comparison stack')
        self.owner = self.root / 'source/pipeline/components/maxwell'
        manifest = self.read(self.owner / 'export-manifest.json')
        require(manifest['source_commit'] == current_graph.SOURCE and
                manifest['owner_version'] == current_graph.VERSION and
                manifest['scientific_image']['pinned_reference'] == current_graph.IMAGE,
                'Selected exact current native source/version/image')
        for path in (self.owner / 'scripts').glob('*.py'):
            require(sha(path) == sha(Path('/opt/ephys-modular/maxwell/scripts') / path.name),
                    'Exact installed scientific tool ' + path.name)
        self.execution = json.loads((self.root / 'reports/execution-and-ports.json').read_text())
        require(sha(self.root / 'reports/execution-and-ports.json') ==
                self.freeze['execution_report_sha256'], 'Completed actual execution report')
        require(self.execution['execution_and_ports_passed'] and
                self.execution['all_18_actual_task_exits_zero'] and
                len(self.execution['complete_phase_observations']) == 4,
                'Four real current long-unit executions')

    def frozen(self, path):
        path = Path(path)
        require(path.is_relative_to(self.root) and path.is_file() and not path.is_symlink(),
                'Scoped long-graph regular file ' + str(path))
        name = str(path.relative_to(self.root))
        require(name in self.freeze['files'], 'Declared long-graph file ' + name)
        row = self.freeze['files'][name]
        require(path.stat().st_size == row['bytes'] and sha(path) == row['sha256'] and
                stat.S_IMODE(path.stat().st_mode) == row['mode'],
                'Exact frozen long-graph file ' + name)
        return path

    def read(self, path):
        return json.loads(self.frozen(path).read_text())

    def uri(self, value):
        parsed = urlsplit(value)
        require(parsed.scheme == 'file' and not parsed.netloc and not parsed.query and
                not parsed.fragment, 'Canonical local long-graph artifact URI')
        return self.frozen(Path(unquote(parsed.path)))


def verify(scope, output, schema_reference):
    output.mkdir(parents=True, exist_ok=False)
    cases = []
    require({(row['kind'], row['alias'], row['case']) for row in scope.execution['complete_phase_observations']} ==
            {('mea', alias, case) for alias in ('alpha', 'beta')
             for case in ('maxone', 'maxtwo_routed')}, 'Complete unique long-case matrix')
    for row in scope.execution['complete_phase_observations']:
        require(row['kind'] == 'mea' and row['case'] in ('maxone', 'maxtwo_routed') and
                row['alias'] in ('alpha', 'beta'), 'Explicit long-graph case scope')
        cases.append(current_graph.compare(scope, row, output, schema_reference,
                                           input_root=scope.root / 'facts'))
        port, = [entry for entry in scope.execution['actual_output_ports']
                 if entry['alias'] == row['alias'] and entry['kind'] == 'mea']
        unit, = [entry for entry in port['actual_ports']
                 if entry['metadata']['unit_id'] == row['case']]
        cases.append(current_graph.compare(scope, row, output, schema_reference,
                                           input_root=scope.root / 'facts',
                                           copy_root=Path(unit['input'])))
        print(row['alias'], row['case'], 'complete native and bridge science passed', flush=True)
    result = dict(passed=True, native_units=4, bridge_copies=4, cases=cases,
                  all_native_invariants_satisfied=True,
                  input_expectations_derived_without_converted_outputs=True,
                  current_consumer_or_sorter_acceptance_claimed=False,
                  original_baseline_acceptance_claimed=False)
    with (output / 'summary.json').open('x') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'output', 'schema-reference'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--trusted-freeze-sha256', required=True)
    args = parser.parse_args()
    import sys
    sys.path.insert(0, '/opt/ephys-modular/maxwell/scripts')
    from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
    configure_bundled_maxwell_hdf5_plugin()
    verify(LongGraphScope(args.root, args.trusted_freeze_sha256), args.output, args.schema_reference)
