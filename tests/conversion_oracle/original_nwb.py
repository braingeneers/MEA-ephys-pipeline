#!/usr/bin/env python3
"""Check original NWB science and characterize every native chunk invariant.

The original producer and immutable independent alias proof remain separate from
this reader. Failed original physical order is reported as failed; native defaults
and complete artifact acceptance are never replaced by this characterization.
"""
from pathlib import Path
import argparse
import importlib.metadata
import json
import sys

import h5py
import numpy as np
from pynwb import NWBHDF5IO, validate

import canonical
import storage
from canonical import require
from inputs import sha, typed

ORIGINAL_SOURCE = '66c5d82c3812b985b0ddc1cccc069f54e2e5297a'
ORIGINAL_IMAGE = 'davidparks21/maxwell-to-nwb@sha256:8486ce5336d922dd81a7f95a40803eb64514a68e37a03d998183284aba11cdaf'


def argument(command, name):
    require(command.count(name) == 1 and command.index(name) + 1 < len(command), 'Exact original argument ' + name)
    return command[command.index(name) + 1]


class FrozenOriginalScope:
    def __init__(self, original, original_freeze_sha256, proof, proof_freeze_sha256):
        self.original, self.proof = original, proof
        require(sha(original / 'freeze.json') == original_freeze_sha256, 'Trusted original freeze identity')
        require(sha(proof / 'freeze.json') == proof_freeze_sha256, 'Trusted baseline-proof freeze identity')
        self.original_freeze = json.loads((original / 'freeze.json').read_text())
        self.proof_freeze = json.loads((proof / 'freeze.json').read_text())
        self.scope = self.read_proof('reports/comparison-scope.json')
        require(self.scope['passed'] is True and self.scope['no_cross_file_or_unreviewed_schema_runtime_mapping'] is True,
                'Complete independent baseline comparison scope')
        require(self.scope['complete_artifact_acceptance'] is False, 'Alias proof is not complete artifact acceptance')
        for name, digest in self.scope['proof_code_sha256'].items():
            require(sha(self.frozen(self.proof, self.proof_freeze, 'code-final/' + name)) == digest,
                    'Independent baseline proof implementation ' + name)
        runtime = self.read_proof('reports/proof-runtime.json')
        actual = sorted([dict(name=d.metadata['Name'], version=d.version) for d in importlib.metadata.distributions()],
                        key=lambda r:r['name'].lower())
        require(actual == runtime['distributions'], 'Exact baseline-proof reader distributions')
        for name, digest in runtime['installed_tool_sha256'].items():
            require(sha(Path('/opt/ephys-modular/maxwell/scripts') / name) == digest, 'Baseline-proof reader tool ' + name)
        from maxwell_h5_utils import configure_bundled_maxwell_hdf5_plugin
        plugin = Path(configure_bundled_maxwell_hdf5_plugin()) / 'libcompression.so'
        require(sha(plugin) == runtime['plugin_sha256'], 'Baseline-proof reader plugin')
        self.producer = self.read_original('identity/original-runtime.json')
        require(self.producer['source_commit'] == ORIGINAL_SOURCE and self.producer['image'] == ORIGINAL_IMAGE,
                'Actual original producer source/image')

    @staticmethod
    def frozen(root, freeze, relative):
        require(not Path(relative).is_absolute() and '..' not in Path(relative).parts, 'Safe frozen relative path')
        path = root / relative
        require(path.is_file() and not path.is_symlink() and relative in freeze['files'], 'Declared frozen object ' + relative)
        row = freeze['files'][relative]
        require(path.stat().st_size == row['bytes'] and sha(path) == row['sha256'], 'Frozen object identity ' + relative)
        return path

    def read_original(self, name):
        return json.loads(self.frozen(self.original, self.original_freeze, name).read_text())

    def read_proof(self, name):
        return json.loads(self.frozen(self.proof, self.proof_freeze, name).read_text())

    def bind(self, option, case):
        require(option in ('enabled', 'disabled') and case in ('maxone', 'maxtwo_routed'), 'Explicit original fixture selection')
        folder = self.original / 'inputs' / case
        facts = self.read_original('inputs/' + case + '/facts.json')
        receipt_name = 'receipts/' + option + '/' + case + '.json'
        receipt = self.read_original(receipt_name)
        nwb = self.frozen(self.original, self.original_freeze, 'published/' + option + '/' + case + '/output.nwb')
        digest = sha(nwb)
        require(digest in self.scope['allowed_files'], 'NWB has no complete independent alias proof')
        binding = self.scope['allowed_files'][digest]
        require(binding['original_frozen_path'] == 'runs/' + option + '/' + case + '/output.nwb', 'Exact original final-NWB proof selection')
        proof = self.read_proof('final-positives/reports/' + binding['report'])
        require(binding['facts_sha256'] == sha(folder / 'facts.json') == proof['facts_sha256'] and
                binding['receipt_sha256'] == sha(self.original / receipt_name) == proof['receipt_sha256'], 'NWB proof facts/receipt binding')
        require(proof['passed'] is True and proof['nwb_sha256'] == digest and proof['original_bytes_unchanged'] is True,
                'Complete unchanged baseline alias proof')
        require(proof['comparison_path_mapping'] == binding['comparison_path_mapping'] and
                proof['distinct_physical_equivalence_pairs'] == binding['distinct_physical_equivalence_pairs'], 'Complete proven path/address mapping')
        require(binding['reader_versions'] == canonical.VERSIONS, 'Pinned alias-proof reader versions')
        require(receipt['case'] == facts['case'] == case and receipt['producer_image'] == ORIGINAL_IMAGE and
                receipt['original_source_commit'] == ORIGINAL_SOURCE and receipt['final_nwb_sha256'] == digest, 'Original scientific ownership/final identity')
        require(receipt['original_source_sha256'] == facts['source']['sha256'] and
                receipt['projection_sha256'] == facts['projection']['sha256'], 'Original authored source/projection identity')
        source = self.frozen(self.original, self.original_freeze, 'inputs/' + case + '/' + facts['source']['file'])
        projection = self.frozen(self.original, self.original_freeze, 'inputs/' + case + '/' + facts['projection']['file'])
        require(sha(source) == facts['source']['sha256'] and sha(projection) == facts['projection']['sha256'], 'Independent source/projection bytes')
        require(typed(json.loads(projection.read_text())) == facts['projection']['original_typed'], 'Original projection numeric types/bits/unknowns')
        selected, = [r for r in facts['stores'] if r['selected']]
        if case == 'maxtwo_routed':
            view = self.frozen(self.original, self.original_freeze, 'views/maxtwo-selected-reference.raw.h5')
            reference = self.read_original('reports/reference-reader-final-proof.json')
            require(receipt['selected_reference'] == reference and reference['passed'] is True and
                    reference['original_source_sha256'] == facts['source']['sha256'] and
                    reference['original_projection_sha256'] == facts['projection']['sha256'] and
                    reference['view_sha256'] == sha(view) == receipt['source_sha256'], 'Complete selected reference binding')
            require(reference['original_max_two_support_claimed'] is False and
                    receipt['original_selected_raw_dataset'] == selected['raw_dataset_path'] == reference['selected_raw_original'] and
                    receipt['selected_raw_dataset'] == reference['selected_raw_view'], 'Preserved original/view routing distinction')
            actual_source = '/evidence/views/maxtwo-selected-reference.raw.h5'
        else:
            require(receipt['selected_reference'] is None and receipt['source_sha256'] == facts['source']['sha256'] and
                    receipt['selected_raw_dataset'] == selected['raw_dataset_path'], 'Original MaxOne source/selector binding')
            actual_source = '/inputs/maxone/recording.raw.h5'
        require(receipt['source_uri'] == Path(actual_source).as_uri(), 'Actual original execution source URI')
        conversion, = [p for p in receipt['phases'] if p['phase'] == 'convert']
        require(conversion['exit'] == 0 and conversion['command'][1] == '/opt/workflow/scripts/convert_maxone_to_nwb.py' and
                argument(conversion['command'], '--input-file') == actual_source and
                argument(conversion['command'], '--maxwell-raw-dataset-path') == receipt['selected_raw_dataset'] and
                argument(conversion['command'], '--nwb-metadata-file') == '/inputs/' + case + '/projection.json',
                'Actual original conversion/source/projection command')
        return folder, facts, receipt, nwb, binding


class OriginalCanonicalCheck(canonical.CanonicalCheck):
    def __init__(self, nodes, binding):
        super().__init__(nodes)
        self.paths = binding['comparison_path_mapping']
        self.addresses = dict(binding['distinct_physical_equivalence_pairs'])
        require(not set(self.addresses.values()) & set(self.addresses), 'Independent alias classes terminate at canonical objects')

    def physical_path(self, path):
        return self.paths.get(path, path)

    def physical_matches(self, actual, expected):
        if actual.file.id != expected.file.id:
            return False
        a = h5py.h5o.get_info(actual.id).addr
        b = h5py.h5o.get_info(expected.id).addr
        return self.addresses.get(a, a) == self.addresses.get(b, b)


def verify(scope, schema_reference):
    require({k:importlib.metadata.version(k) for k in canonical.VERSIONS} == canonical.VERSIONS, 'Pinned canonical reader versions')
    require({k:importlib.metadata.version(k) for k in storage.STORAGE_VERSIONS} == storage.STORAGE_VERSIONS, 'Pinned physical reader versions')
    from maxwell_lindi_codec import MaxwellMxwDataCodec
    decoder = MaxwellMxwDataCodec()
    results = []
    with h5py.File(schema_reference, 'r') as reference:
        for option in ('enabled', 'disabled'):
            for case in ('maxone', 'maxtwo_routed'):
                folder, facts, receipt, nwb_path, binding = scope.bind(option, case)
                nodes, selected, clock = canonical.expected_graph(folder, facts, receipt)
                errors = validate(path=str(nwb_path))
                require(not errors, 'Original NWB schema errors: ' + str(errors))
                oracle = OriginalCanonicalCheck(nodes, binding)
                with NWBHDF5IO(str(nwb_path), 'r', load_namespaces=True) as io:
                    nwb = io.read()
                    oracle.bind(nwb)
                    oracle.check_fields()
                    series = oracle.objects['signal']
                    actual_clock = series.starting_time + np.arange(series.data.shape[0], dtype='<f8') / series.rate
                    oracle.compare(actual_clock, clock, 'Complete original acquisition clock')
                    require(float(series.conversion).hex() == selected['expected_nwb_conversion_hex'] and
                            float(series.offset).hex() == selected['offset_hex'], 'Exact original calibrated gain/offset')
                    with h5py.File(nwb_path, 'r') as handle:
                        paths = oracle.physical(handle, io)
                        schema_nodes = storage.schema_cache(handle, reference)
                        sidecar_path = nwb_path.with_name('output.nwb.lindi.json')
                        sidecar = json.loads(sidecar_path.read_text())
                        truth = canonical.load_array(folder, selected['signal'])
                        rows, padding, ordered = storage.native_chunk_observations(handle, nwb_path, truth, selected,
                            receipt['settings'], sidecar['refs'], receipt['reader_url'], sidecar.get('templates', {}), decoder)
                results.append(dict(option=option, case=case, nwb_sha256=sha(nwb_path),
                    facts_sha256=sha(folder / 'facts.json'), receipt_sha256=binding['receipt_sha256'], schema_valid=True,
                    canonical_nodes=len(nodes), Data_payloads=13, physical_contract_paths=paths,
                    scalar_comparisons=oracle.scalars, exact_numeric_values_checked=oracle.numeric_values,
                    canonical_and_physical_reference_comparisons=oracle.references, physical_aliases_checked=oracle.physical_links,
                    schema_cache_nodes=schema_nodes, native_chunks=rows, padding_values_checked=padding,
                    native_channel_major_physical_order_satisfied=ordered,
                    actual_physical_order=[r['coordinate'] for r in sorted(rows, key=lambda r:r['offset'])],
                    required_channel_major_order=[r['coordinate'] for r in rows],
                    failed_native_invariants=[] if ordered else ['Complete channel-major physical allocation order'],
                    original_scientific_values_match=True, all_chunk_ranges_values_masks_padding_and_nonoverlap_checked=True,
                    baseline_mapping_applied=binding['comparison_path_mapping'], original_source_sha256=receipt['original_source_sha256'],
                    actual_execution_source_sha256=receipt['source_sha256'], actual_selected_raw_dataset=receipt['selected_raw_dataset'],
                    original_nextflow_orchestration_executed=False, complete_artifact_acceptance=False))
                print(option, case, 'complete science/schema/chunks; native order', ordered, flush=True)
    manifest = scope.read_original('inputs/input-set.json')
    require(len(manifest['cases']) == 2 and {r['case'] for r in manifest['cases']} == {'maxone', 'maxtwo_routed'}, 'Complete independent original selection inventory')
    for row in manifest['cases']:
        require(sha(scope.original / 'inputs' / row['case'] / 'facts.json') == row['facts_sha256'], 'Original input manifest fact identity')
    recordings = scope.read_original('inputs/recordings.json')
    bindings = scope.read_original('inputs/bindings.json')
    require(scope.read_original('inputs/transport-facts.json') == dict(recordings=typed(recordings), bindings=typed(bindings)),
            'Complete original transport JSON encodings/unknowns')
    return dict(completed=True, scope='Original NWB science/schema and complete native chunk characterization only',
                cases=results, all_scientific_values_match=True, all_native_invariants_satisfied=all(not r['failed_native_invariants'] for r in results),
                schema_reference_sha256=sha(schema_reference), original_producer_source=ORIGINAL_SOURCE, original_producer_image=ORIGINAL_IMAGE,
                complete_artifact_acceptance=False, original_parent_04e3c3_complete=False,
                native_checker_defaults_relaxed=False,
                limits=['Observed failed original physical order remains a failed native invariant.',
                        'Complete original sidecars, companions, reports, publication and full parent audit remain separate.',
                        'The proven selected raw view and exact within-file mappings do not establish original MaxTwo or writable HDMF support.'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('original', 'proof', 'schema-reference', 'report'):
        parser.add_argument('--' + name, required=True, type=Path)
    for name in ('original-freeze-sha256', 'proof-freeze-sha256'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    sys.path.insert(0, '/opt/ephys-modular/maxwell/scripts')
    scope = FrozenOriginalScope(args.original, args.original_freeze_sha256, args.proof, args.proof_freeze_sha256)
    result = verify(scope, args.schema_reference)
    with args.report.open('x') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
