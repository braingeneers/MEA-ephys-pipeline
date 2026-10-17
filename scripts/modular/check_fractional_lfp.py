#!/usr/bin/env python3
"""Exercise the original LFP regression on paired fractional signed/unsigned NWBs."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys


def main():
    import numpy as np
    import spikeinterface as si
    import spikeinterface.extractors as se
    from neuroconv.tools.spikeinterface import add_recording_to_nwbfile
    from pynwb import NWBFile, NWBHDF5IO

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[2]
    results = {}
    for name in ("signed", "unsigned"):
        recording = se.read_nwb_recording(args.fixture / name / "synthetic.nwb")
        part = recording.frame_slice(start_frame=0, end_frame=int(recording.get_sampling_frequency() * 2.06))
        path = args.output / (name + ".nwb")
        nwb = NWBFile(session_description="Fractional-tail regression fixture",
                      identifier="fractional-" + name,
                      session_start_time=datetime(2026, 1, 1, tzinfo=timezone.utc))
        add_recording_to_nwbfile(part, nwbfile=nwb)
        assert len(nwb.acquisition) == 1
        series_path = "acquisition/" + next(iter(nwb.acquisition))
        with NWBHDF5IO(path, "w") as writer:
            writer.write(nwb)
        target = args.output / name
        subprocess.run([sys.executable, str(source / "scripts/nrp/check_lfp_tail.py"),
                        "--kind", "nwb", "--input", str(path), "--output", str(target),
                        "--electrical-series", series_path], check=True)
        results[name] = json.loads((target / "result.json").read_text())
    signed = si.load(args.output / "signed/tail").get_traces()
    unsigned = si.load(args.output / "unsigned/tail").get_traces()
    assert np.array_equal(signed, unsigned), "Signed/unsigned LFP samples differ"
    result = {"passed": True, "signed_unsigned_samples_equal": True, "checks": results}
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
