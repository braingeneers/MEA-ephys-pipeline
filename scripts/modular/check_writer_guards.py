#!/usr/bin/env python3
"""Exercise portable writer admission in the pinned NWB image, offline.

Provide old expanded-reference and verified metadata-refresh fixtures. This tests
writer admission and core backend policies; full scientific producers are separate.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
from pynwb import NWBFile, NWBHDF5IO, TimeSeries
from hdmf_zarr import NWBZarrIO

ROOT = Path(__file__).resolve().parents[2]
READER = ROOT / 'capsules/nwb_common/code'
GUARD = ROOT / 'pipeline/components/maxwell/runtime'
sys.path[:0] = [str(READER), str(GUARD)]
from source_nwb import open_source_nwb


def hashes(path):
    files = [path] if path.is_file() else sorted(p for p in path.rglob('*') if p.is_file())
    return {str(p.relative_to(path) if path.is_dir() else p.name): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bad', type=Path, action='append', required=True)
    parser.add_argument('--refreshed', type=Path, action='append', required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=False)
    outcomes = []
    for admitted, paths in ((False, args.bad), (True, args.refreshed)):
        for path in paths:
            before = hashes(path)
            try:
                with open_source_nwb(path, 'r', NWBHDF5IO, for_writing=True) as io:
                    nwb = io.read()
                    assert len(nwb.subject.culture.parent_cultures[:]) == 2
            except ValueError as error:
                assert not admitted and 'NWB write blocked' in str(error)
                assert 'Keep the stored NWB and its evidence unchanged' in str(error)
                outcomes.append({'file': str(path), 'admitted': False, 'error': str(error)})
            else:
                assert admitted
                outcomes.append({'file': str(path), 'admitted': True})
            assert hashes(path) == before
    data = np.arange(24, dtype=np.float64).reshape(12, 2)
    backends = []
    for label, cls, suffix in (('hdf5', NWBHDF5IO, '.nwb'), ('zarr', NWBZarrIO, '.zarr')):
        core = NWBFile('Core backend control', 'core-writer-control', datetime(2026, 10, 18, tzinfo=timezone.utc))
        core.add_acquisition(TimeSeries(name='signal', data=data, unit='volts', rate=1000.0))
        path = args.work / ('core' + suffix)
        with cls(str(path), 'w') as io:
            io.write(core)
        before = hashes(path)
        for policy in ('export', 'append'):
            target = args.work / (label + '-' + policy + suffix)
            with open_source_nwb(path, 'r', cls, for_writing=True) as reader:
                nwb = reader.read()
                if policy == 'export':
                    with cls(str(target), 'w') as writer:
                        writer.export(src_io=reader, nwbfile=nwb,
                                      write_args={'link_data': False} if label == 'zarr' else {})
                else:
                    if path.is_dir():
                        shutil.copytree(path, target)
                    else:
                        shutil.copyfile(path, target)
            if policy == 'append':
                with open_source_nwb(target, 'a', cls) as writer:
                    writer.write(writer.read())
            with open_source_nwb(target, 'r', cls) as reader:
                actual = np.asarray(reader.read().acquisition['signal'].data[:])
                assert actual.dtype == data.dtype and actual.shape == data.shape and actual.tobytes() == data.tobytes()
            backends.append({'backend': label, 'policy': policy, 'values': data.size})
        assert hashes(path) == before
    # The reader is deliberately staged alone. Writing must fail before opening
    # even a missing source; read-only import remains independently usable.
    isolated = args.work / 'missing-helper'
    isolated.mkdir()
    shutil.copyfile(READER / 'source_nwb.py', isolated / 'source_nwb.py')
    command = [sys.executable, '-I', '-c',
               'import sys;sys.path.insert(0,sys.argv[1]);from source_nwb import open_source_nwb;'
               'from pynwb import NWBHDF5IO;open_source_nwb(sys.argv[2],"a",NWBHDF5IO)',
               str(isolated), str(isolated / 'must-not-be-created.nwb')]
    process = subprocess.run(command, capture_output=True, text=True)
    assert process.returncode != 0 and "No module named 'nwb_graph'" in process.stderr
    assert not (isolated / 'must-not-be-created.nwb').exists()
    report = {'passed': True, 'modeled': outcomes, 'core_roundtrips': backends,
              'missing_helper_rejected_before_open': True, 'scientific_producer_clis': 0}
    (args.work / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
