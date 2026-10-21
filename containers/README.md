# Workflow runtime images

Both callers resolve task images from
[`pipeline/container_images.json`](../pipeline/container_images.json), through
shared invocation settings. [`runtime-images.json`](runtime-images.json) records
the exact local builds and owning recipes. This development branch integrates
these builds locally; registry publication and immutable registry digest pins
remain pending. Versioned references must resolve to the listed local IDs during
development validation. They are not yet published releases.

| Role | Image | Build path |
| --- | --- | --- |
| Base and earlier bundle boundaries | `braingeneers/mea-ephys-base:0.1.0` | [Offline codec](maxwell-codec/README.md) on the original scientific base |
| Exported bundle boundaries | `braingeneers/mea-ephys-export-verifier:0.1.0-candidate` | [Compatible reader](export-verifier/README.md), preserving the base numerical stack; full stage release pending |
| KS4 | `braingeneers/mea-ephys-kilosort4:0.1.0` | Same codec derivation on the original KS4 image |
| Reports | `braingeneers/mea-ephys-analysis:0.2.0` | [Spreadsheet dependencies](analysis/README.md), then offline codec |
| NWB writers | `braingeneers/mea-ephys-nwb:0.2.2` | [Reader dependencies](nwb-io/README.md), then [backend patches](nwb-backend/README.md) |

Base/KS4/report codec layers preserve every parent Python package. The report
parent adds exactly openpyxl 3.1.5 and et-xmlfile 2.0.0. The NWB reader changes
PyNWB 2.8.2 → 4.0.0, HDMF 3.14.5 → 6.1.0, NeuroConv 0.6.7 → 0.7.0 and hdmf-zarr
0.9.0 → 0.12.0, and adds ndx-cell-culture 1.0.0 and platformdirs 4.2.2. Its other
104 distributions are unchanged. The final NWB layer patches only two exact
modules and their RECORDs; all 110 versions and numerical packages remain fixed.
Each recipe locks its complete input and patch hashes.

Build the report parent before its codec layer. Stage the six locked NWB I/O wheels
as documented before building its backend child. Build coordinators record exact
commands, source hashes, local IDs, parent-layer checks and offline plugin evidence
in fresh external output files. All final images contain the same pinned filter-401
plugin. Tasks install no packages and need no public plugin download or host mount.

For local development, supply a Nextflow params file such as:

```json
{
  "container_images": {
    "base": "braingeneers/mea-ephys-base:0.1.0",
    "ks4": "braingeneers/mea-ephys-kilosort4:0.1.0",
    "nwb": "braingeneers/mea-ephys-nwb:0.2.2",
    "export_verifier": "braingeneers/mea-ephys-export-verifier:0.1.0-candidate",
    "analysis": "braingeneers/mea-ephys-analysis:0.2.0"
  }
}
```

Use Nextflow's `-params-file`, or pass the same map to `loadLaunchSettings` for an
imported stage. Unspecified roles inherit defaults. The existing
`--report_container_image` option overrides only `analysis`, including when both
controls are supplied. Image maps are separate from scientific `--params_file`
JSON. Unknown roles, non-map overrides and empty/non-string image values fail
before tasks start. KS2.5 retains its original SI-versioned image; other sorters
retain their original capsule pins and scientific base stack. Callers own the
compatibility of custom image overrides.

Only exported bundle boundaries select `export_verifier`; earlier boundaries use
`base` and scientific NWB writers keep `nwb`. Each role remains invocation-local.
The candidate's exact package/file and existing scientific receipt compatibility
is recorded beside its recipe. Its complete stage release remains a separate gate.

Docker and NRP selectors follow each task's invocation settings. NERSC retains
independent Shifter configuration and Nextflow 23. Common images do not establish
real NERSC/Shifter or GPU acceptance. Older per-build image records retain their
observed build-time scope; this index identifies the configured accepted artifacts.
Do not retag a different build as a recorded version. Publication must verify every
exact local ID, freeze registry digests in shared defaults and the NRP helper map,
and retain parent publication dependencies before advertising the contribution.
