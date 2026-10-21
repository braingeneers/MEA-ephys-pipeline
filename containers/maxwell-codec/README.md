# Offline Maxwell codec images

These derived images add one checksum-verified `libcompression.so` and configure
`HDF5_PLUGIN_PATH` inside each scientific image. They retain its Python packages,
scientific stack, command, other environment and `pipelineuser`. The binary comes
from the pinned public converter image; no workstation plugin mount or task-time
download is required. Its SHA-256 is
`4bc74ea98c08b70406aa976950fcf5f55d572430f51d3882eeb947c557dd14f4`, 35,776 bytes.
The maintained converter installer records the vendor download provenance in
`pipeline/components/maxwell/scripts/install_maxwell_hdf5_plugin.py`.

## Local development builds

The accepted base/KS4/report builds are published and selected by immutable digest.
[The runtime index](../runtime-images.json) identifies their exact IDs and the final
NWB backend child. The original
three registry parents are digest-pinned in `build.py`. The report parent is the
unchanged [report image recipe](../analysis/README.md). Pull the published parent
before reproducing the historical build:

```bash
docker pull braingeneers/mea-ephys-analysis:0.1.0
python3 containers/maxwell-codec/build.py \
  --report-parent "$(docker image inspect --format '{{.Id}}' braingeneers/mea-ephys-analysis:0.1.0)" \
  --output /data/codec-build.json
```

Omitting `--report-parent` selects the exact original local image ID recorded in
`containers/analysis/image.json`. A rebuilt parent can have a different content ID;
the build record freezes the actual immutable input. Mutable parent tags are
rejected. The three other parents and pinned plugin-source image must also be
available locally for the offline builds. `--roles base ks4 analysis nwb` selects
roles explicitly. Use a fresh build-record filename for each invocation.
The builder retains its historical fixed tag assignments. For a new build, select
distinct development/version tags in its `REFERENCES` map before running it; keep
published release tags bound to their recorded images.
BuildKit needs a named `FROM` reference for an unpublished image. The script gives
a raw report content ID a local content-named alias, verifies that alias before
and after the build, and checks the child's exact parent-layer prefix. Both the
immutable parent ID and the build alias appear in the record.

The four output names are `braingeneers/mea-ephys-base:0.1.0`,
`braingeneers/mea-ephys-kilosort4:0.1.0`, `braingeneers/mea-ephys-analysis:0.2.0` and
`braingeneers/mea-ephys-nwb:0.1.0`. The record captures exact local image IDs and
build commands; publication must freeze registry digests before release use.
The codec-only NWB 0.1.0 image is historical, not the configured writer default.
Codec tests alone do not prove modeled NWB read/write,
real exporter compatibility or complete conversion/scientific parity.
