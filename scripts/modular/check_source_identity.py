#!/usr/bin/env python3
"""Check native identity on retained real stage bundles without copying science.

Run in the pinned base image with --bundle for both Maxwell layouts and positive/
zero controls. --wrong-source is an existing independently resealed bad bundle;
this check never changes it. Native modeled validation is checked separately in
the converter image. These checks do not replace independent scientific oracles.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / 'pipeline/helpers'))
import local_conversion_identity as identity
import portable_bundle as portable
import stage_bundle as stages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, action='append', required=True)
    parser.add_argument('--wrong-source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    contracts = identity.load_contracts()
    no_spikes = stages.classifier(SOURCE / 'capsules/kilosort4/code/outcomes.py')
    results = []
    for root in args.bundle:
        record = stages.read_record(root)
        unit_meta = record['metadata']['unit_meta']
        native = root / 'data/ecephys_session'
        before = portable.inventory(root / 'data')
        actual = stages.verify(root, record['stage'], record['unit_id'], no_spikes,
                               unit_meta=unit_meta)
        assert portable.encoded(actual) == portable.encoded(record)
        receipt = identity.verify_identity(native, unit_meta=unit_meta)
        extended = deepcopy(unit_meta)
        extended['source_identity_check'] = {'integer': 2**80, 'zero': -0.0, 'unicode': 'μV'}
        identity.verify_identity(native, unit_meta=extended)
        results.append(dict(bundle=str(root), case='unchanged-and-extended-metadata', passed=True))
        # The receipt, not a reserialized expectation, owns the original encodings.
        for value in (True, float(1), None, {}, []):
            changed = deepcopy(unit_meta)
            changed['maxwell_conversion'] = value
            try:
                identity.verify_identity(native, unit_meta=changed)
            except contracts.ContractError as error:
                assert str(error) == 'Stage unit metadata differs from its native conversion receipt'
            else:
                raise AssertionError('Changed native metadata accepted')
            results.append(dict(bundle=str(root), case='changed-native-metadata', value=value, passed=True))
        changed = deepcopy(unit_meta)
        changed['maxwell_conversion']['schema_version'] = 1.0
        try:
            identity.verify_identity(native, unit_meta=changed)
        except contracts.ContractError as error:
            assert str(error) == 'Stage unit metadata differs from its native conversion receipt'
        else:
            raise AssertionError('Floating schema metadata accepted')
        results.append(dict(bundle=str(root), case='native-metadata-integer-versus-float', passed=True))
        assert identity.json_equal(receipt['unit_meta']['maxwell_conversion'],
                                   unit_meta['maxwell_conversion'])
        job = stages.jobs(root / 'data')[0]
        graph = job['recording_dict']
        expected_nwb = (native / 'output.nwb').resolve(strict=True)
        assert stages.verify_native_graph(graph, root / 'data', expected_nwb) > 0
        history = deepcopy(graph)
        history.setdefault('annotations', {})['historical_recording'] = {
            'class': 'spikeinterface.extractors.nwbextractors.NwbRecordingExtractor',
            'kwargs': {'file_path': '/unavailable/historical/output.nwb'}}
        assert stages.verify_native_graph(history, root / 'data', expected_nwb) > 0
        results.append(dict(bundle=str(root), case='historical-path-is-not-executable', passed=True))
        rebound = deepcopy(graph)

        def substitute(value):
            if isinstance(value, list):
                for child in value:
                    substitute(child)
            elif isinstance(value, dict):
                if value.get('class', '').endswith('.NwbRecordingExtractor'):
                    value['kwargs']['file_path'] = 'ecephys_session/output.nwb.lindi.json'
                substitute(value.get('kwargs', {}) if 'class' in value else list(value.values()))

        substitute(rebound)
        try:
            stages.verify_native_graph(rebound, root / 'data', expected_nwb)
        except portable.BundleError as error:
            assert str(error) == 'Active NWB recording differs from its native conversion source'
        else:
            raise AssertionError('Active NWB path rebound accepted')
        results.append(dict(bundle=str(root), case='active-NWB-rebound', passed=True))
        assert portable.inventory(root / 'data') == before
    # Generic portable inputs have no native contract dependency.
    stages.verify_native_conversion(Path('/unavailable'), {'metadata': {'unit_meta': {}}})
    results.append(dict(case='generic-native-guard-opt-out', passed=True))
    try:
        identity.load_contracts(args.output / 'absent-common')
    except ValueError as error:
        assert str(error) == 'Missing declared native contract/schema tree'
    else:
        raise AssertionError('Invalid explicit common root fell back to installed code')
    results.append(dict(case='invalid-explicit-common-no-fallback', passed=True))
    if args.wrong_source:
        root = args.wrong_source
        before = portable.inventory(root / 'data')
        record = stages.read_record(root)
        message = 'Native conversion identity: Local sibling differs from its native published object'
        try:
            stages.verify(root, record['stage'], record['unit_id'], no_spikes,
                          unit_meta=record['metadata']['unit_meta'])
        except portable.BundleError as error:
            assert str(error) == message
        else:
            raise AssertionError('Retained wrong scientific source accepted')
        command = [sys.executable, str(SOURCE / 'pipeline/helpers/stage_bundle.py'),
                   '--outcomes-code', str(SOURCE / 'capsules/kilosort4/code/outcomes.py'),
                   'verify', '--bundle', str(root), '--kind', record['stage'],
                   '--unit-id', record['unit_id']]
        cli = subprocess.run(command, text=True, capture_output=True)
        assert cli.returncode == 1 and message in cli.stderr, cli
        assert portable.inventory(root / 'data') == before
        results.append(dict(case='retained-wrong-source-API-and-CLI', passed=True,
                            command=command, exit=cli.returncode, stderr=cli.stderr))
    (args.output / 'summary.json').write_text(json.dumps(dict(passed=True, cases=results,
        scope='Read-only source identity, metadata and active graph controls; no new science'), indent=2) + '\n')


if __name__ == '__main__':
    main()
