"""Create/validate the reader fixture in the pinned native NWB image.

The base scientific image only consumes this fixture; it does not import PyNWB.
Usage: python tests/create_identity_nwb.py /evidence/identity.nwb
"""
from datetime import datetime, timezone
from pathlib import Path
import sys

import numpy as np
from hdmf.backends.hdf5.h5_utils import H5DataIO
from pynwb import NWBFile, NWBHDF5IO, validate
from pynwb.ecephys import ElectricalSeries


output = Path(sys.argv[1])
assert not output.exists()
nwb = NWBFile("Identity reader regression", "mea-identity-reader",
              datetime(2026, 1, 1, tzinfo=timezone.utc))
device = nwb.create_device("MEA")
group = nwb.create_electrode_group("electrodes", "Reader fixture", "culture", device)
nwb.add_electrode_column("channel_name", "Acquisition channel name")
nwb.add_electrode_column("rel_x", "Relative electrode x coordinate in micrometers")
nwb.add_electrode_column("rel_y", "Relative electrode y coordinate in micrometers")
for channel in range(16):
    nwb.add_electrode(id=channel, x=channel * 17.5, y=0.0, z=0.0,
                      imp=float("nan"), location="culture", filtering="none",
                      group=group, channel_name=f"ch{channel}", rel_x=channel * 17.5, rel_y=0.0)
frames = np.arange(131251, dtype="int64")[:, None]
channels = np.arange(16, dtype="int64")[None, :]
values = ((frames * 17 + channels * 3) % 2048 - 1024).astype("int16")
nwb.add_acquisition(ElectricalSeries(
    name="ElectricalSeries", data=H5DataIO(values, chunks=(10000, 1), compression="gzip"),
    electrodes=nwb.create_electrode_table_region(list(range(16)), "All channels"),
    starting_time=1.25, rate=10000.0, conversion=0.195e-6,
    channel_conversion=1.0 + np.arange(16) * 0.01, offset=-0.0001))
with NWBHDF5IO(str(output), "w") as io:
    io.write(nwb)
errors = validate(path=str(output))
assert not errors, errors
print(f"VALIDATED_IDENTITY_FIXTURE {output} {values.shape}")
