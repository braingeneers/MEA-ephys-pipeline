#!/usr/bin/env python3
"""Reject declared contract/schema trees that differ from an existing import."""
import argparse
import json
from pathlib import Path
import shutil
import sys

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / 'pipeline/helpers'))
import local_conversion_identity as identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    original = SOURCE / 'pipeline/components/maxwell/components/common/python'
    contracts = identity.load_contracts(original)
    cases = []
    for kind, message in (
            ('code', 'Loaded native contract code differs from the declared tree'),
            ('schema', 'Loaded native contract schema differs from the declared tree')):
        root = args.output / kind
        shutil.copytree(original / 'ephys_modular', root / 'ephys_modular')
        if kind == 'code':
            path = root / 'ephys_modular/contracts.py'
            path.write_bytes(path.read_bytes() + b'\n# Different declared contract source.\n')
        else:
            path = root / 'ephys_modular/schemas/artifacts-v1.schema.json'
            schema = json.loads(path.read_text())
            schema['title'] = str(schema.get('title', '')) + ' different declared schema'
            path.write_text(json.dumps(schema))
        try:
            identity.load_contracts(root)
        except ValueError as error:
            assert str(error) == message, str(error)
        else:
            raise AssertionError('Different declared tree reused a cached import')
        assert identity.load_contracts(original) is contracts
        cases.append(dict(case=kind, rejected=True, error=message,
                          original_import_retained=True))
    for name in ('ndx_cell_culture', 'pynwb', 'h5py', 'spikeinterface', 'botocore',
                 'conversion_worker', 'task_preparation'):
        assert name not in sys.modules, name
    (args.output / 'summary.json').write_text(json.dumps(dict(passed=True, cases=cases,
        scientific_or_storage_modules_imported=False), indent=2) + '\n')


if __name__ == '__main__':
    main()
