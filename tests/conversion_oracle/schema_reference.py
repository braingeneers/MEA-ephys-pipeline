#!/usr/bin/env python3
"""Create an independent schema-cache reference from the pinned installed catalog."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import ndx_cell_culture
from pynwb import NWBFile, NWBHDF5IO

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Schema reference requires a new output path')
    reference = NWBFile('Independent installed schema cache', 'schema-cache-reference',
                        datetime(2026, 10, 19, tzinfo=timezone.utc))
    with NWBHDF5IO(str(args.output), 'w') as io:
        io.write(reference)
    print(args.output)
