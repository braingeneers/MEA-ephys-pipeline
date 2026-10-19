#!/usr/bin/env python3
"""Compile input-only complete NWB metadata expectations or verify a copied store."""
import argparse
import json
from pathlib import Path
import sys

from nwb_metadata_guard import compile_expected, verify_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--helpers', type=Path, help='Exact original source_nwb/nwb_graph helper closure, when checking historical artifacts')
    commands = parser.add_subparsers(dest='command', required=True)
    compile_parser = commands.add_parser('compile')
    compile_parser.add_argument('--source', type=Path, required=True)
    compile_parser.add_argument('--facts', type=Path, required=True)
    compile_parser.add_argument('--arrays', type=Path, required=True)
    compile_parser.add_argument('--job', type=Path, required=True)
    compile_parser.add_argument('--sorter-log', type=Path)
    compile_parser.add_argument('--scratch', type=Path, required=True)
    compile_parser.add_argument('--output', type=Path, required=True)
    verify_parser = commands.add_parser('verify')
    verify_parser.add_argument('--nwb', type=Path, required=True)
    verify_parser.add_argument('--expected', type=Path, required=True)
    verify_parser.add_argument('--expected-data', type=Path, required=True)
    verify_parser.add_argument('--role', choices=('nwb_ecephys', 'nwb_units'), required=True)
    verify_parser.add_argument('--write-raw', action='store_true')
    verify_parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.helpers:
        sys.path.insert(0, str(args.helpers))
    else:
        root = Path(__file__).resolve().parents[2]
        sys.path[:0] = [str(root / 'capsules/nwb_common/code'),
                        str(root / 'pipeline/components/maxwell/runtime')]
    if args.command == 'compile':
        record = compile_expected(args.source, json.loads(args.facts.read_text()), args.arrays,
                                  args.job, args.sorter_log, args.scratch)
        output = args.output
    else:
        from hdmf_zarr import NWBZarrIO
        from pynwb import validate
        from source_nwb import open_source_nwb
        from nwb_data_guard import verify_store as verify_data
        with open_source_nwb(args.nwb, 'r', NWBZarrIO, for_writing=True) as io:
            nwb = io.read()
            record = verify_store(args.nwb, nwb, json.loads(args.expected.read_text()), args.role, args.write_raw)
            record['stored_data'] = verify_data(args.nwb, nwb, json.loads(args.expected_data.read_text()), args.role, args.write_raw)
            errors = validate(io=io)
            if errors:
                raise AssertionError(errors)
        record['schema_valid'] = True
        output = args.report
    with output.open('x') as stream:
        stream.write(json.dumps(record, indent=2) + '\n')
    print(json.dumps(dict(passed=True, command=args.command, report=str(output))))


if __name__ == '__main__':
    main()
