# Export verifier runtime candidate

This recipe is a local candidate for complete exported-NWB verification. Workflow
defaults continue to use their existing images until full boundary acceptance.
It derives from `braingeneers/mea-ephys-base:0.1.0`, preserving that image's NumPy,
SciPy, Pandas, SpikeInterface, HDF5 and Zarr packages. It updates only HDMF,
HDMF-Zarr and PyNWB and adds the required platformdirs package, using the four
checksum-locked artifacts in `artifacts.lock.json`. The scientific writer image
retains its separate dependency stack.

The original base cannot import the older HDMF-Zarr reference codec under its
installed HDMF. The existing writer image decodes complete references but changes
the measured analyzer spike-vector identity. This candidate must decode complete
NWB model/Data/reference fields while retaining every original scientific
fingerprint. No fingerprint tolerance, replacement reference class or scientific
recomputation is part of this repair.

Stage exactly the four locked wheels under `wheelhouse/` in a fresh external build
context with these recipe files. The wheels stay outside Git. Check the exact
parent image ID against the lock before and after building. Use an actual clean
recipe revision for the label; freeze development inputs explicitly otherwise.

For local development, build from that context with the classic Docker builder,
which applies the requested limits to its installation container:

```bash
DOCKER_BUILDKIT=0 docker build --network none --memory 8g \
  --cpu-period 100000 --cpu-quota 200000 \
  --build-arg MEA_SOURCE_REVISION="$MEA_RECIPE_REVISION" \
  --tag braingeneers/mea-ephys-export-verifier:0.1.0-candidate .
```

Resolve `MEA_RECIPE_REVISION` with `git rev-parse HEAD` in the clean recipe checkout.
Installation verifies the real cgroups, wheel hashes, exact package version delta,
`pip check`, concrete reader imports and inherited filter401 plugin. The image
restores `pipelineuser`. Retain build logs, the exact local ID, recipe revision and
parent layers, then verify every unchanged nonbytecode package file and complete
scientific receipts in default-user runtime containers. Build success alone is
not full scientific or Nextflow acceptance.

This candidate is unpublished. Root contribution publication must assign and
verify its immutable registry identity before advertising portable launch examples.
