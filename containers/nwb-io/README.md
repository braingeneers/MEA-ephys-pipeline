# Modeled NWB reader build parent

`braingeneers/mea-ephys-nwb:0.2.0` is the published build parent for the configured
[0.2.2 backend runtime](../nwb-backend/README.md). The runtime index records both
immutable digests. Typed reads pass for genuine
converter-produced MaxOne/MaxTwo NWBs with core2.10 and ndx-cell-culture1.0, and
for older plain HDF5/Zarr files. Its original build checks retain that reader scope;
complete modeled writer/independent acceptance belongs to the final backend child.

The original digest-pinned NWB image remains the parent. This recipe adds the
same checksum-verified Maxwell plugin as [the codec images](../maxwell-codec/README.md)
and makes these explicit NWB I/O dependency changes:

| Package | Original | Candidate | Reason |
| --- | --- | --- | --- |
| PyNWB | 2.8.2 | 4.0.0 | Core2.10 typed readers |
| HDMF | 3.14.5 | 6.1.0 | PyNWB4 requirement |
| NeuroConv | 0.6.7 | 0.7.0 | Remove its older HDMF upper bound |
| hdmf-zarr | 0.9.0 | 0.12.0 | Compatible concrete Zarr namespace-loading API |
| ndx-cell-culture | absent | 1.0.0 | Exact converter extension source/classes/schema |
| platformdirs | absent | 4.2.2 | PyNWB4 dependency |

All 104 other installed distributions and their 12,897 inventoried non-bytecode
files match the original parent exactly, including NumPy1.26.4, SciPy1.17.1 and
SpikeInterface0.103.2. No sorter, numerical, scientific-default or task-resource
change is made. The base image's independent optional PyNWB import mismatch is
outside this writer-image repair.

## Build

For a new build, select a new image version in `artifacts.lock.json` before running
the historical builder. Published release tags identify the recorded image builds.

Run from the repository root. Docker and the two public parent/plugin images in
`artifacts.lock.json` must be available. The first step runs inside Docker and
downloads only checksum-locked public artifacts. Use fresh paths outside Git:

```bash
python3 containers/nwb-io/build.py \
  --work-dir /data/nwb-io-build-01 \
  --output /data/nwb-io-build-01.json
```

The extension is built from Git commit
`4691d789120b397eb1aa4a6169f1ed353fecaca8`, matching the pinned native converter.
Its source archive, build tools and resulting reproducible wheel are all locked
by checksum. Build tools stay in the preparation directory. The final Docker
build has networking disabled and installs six locked wheels with `--no-index`,
`--no-deps` and `--require-hashes`. It checks dependencies and the actual APIs
imported by both writer capsules, then restores `pipelineuser`.

`image.json` records the verified local image ID, recipe/artifact hashes and scope.
A registry digest must be recorded on publication. No dependency/plugin download
or workstation plugin mount is needed by a scientific task.

## Read verification

Both real writer CLIs pass import/`--help` with HOME=/tmp. In network-disabled,
UID1000 containers, the candidate reads all 17,600 neural values, complete clocks,
gains/offsets, channels, geometry and HDF5 references from the two accepted tiny
converter fixtures, matching the independently frozen native reference. It also
reads all 2,048 original codec values and encodes/decodes another 2,048 values in
32 allocated filter401 chunks. The pinned native validator independently accepts
both schemas and every authored modeled scalar/reference. Inputs stay unchanged.

Keep fixture NWBs, complete authored projection JSON and results outside Git.
The reusable check below runs in this image and creates corrupted copies only
under a fresh work directory; mount the repository and fixtures read-only:

```bash
docker run --rm --network none --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD:/source:ro" -v /data/tiny-fixtures:/fixtures:ro \
  -v /data/reader-results:/results --entrypoint python \
  braingeneers/mea-ephys-nwb:0.2.0 /source/scripts/modular/check_nwb_reader.py \
  --modeled /fixtures/maxone.nwb /fixtures/maxone-projection.json \
  --modeled /fixtures/maxtwo_routed.nwb /fixtures/maxtwo_routed-projection.json \
  --work-dir /results/case-01 --output /results/case-01.json
```

This checks every modeled field/reference against its projection, complete typed
signals/clocks/electrodes, source schema validation, wrong authored values, newer
core, incompatible modeled extension and unknown namespace rejection. It rereads
the original after failures to check namespace isolation. Separate retained
controls cover old plain HDF5/Zarr and fixed-byte legacy version headers. These
are reader checks; real producer runs remain a distinct acceptance gate.
