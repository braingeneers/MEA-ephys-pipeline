# Owning-manager and schema-cache NWB candidate

`braingeneers/mea-ephys-nwb:0.2.2` is the configured local writer image, derived
from the exact accepted [0.2.0 reader image](../nwb-io/README.md). Registry publication
is pending; [the runtime index](../README.md) records this state. It retains every
parent layer, package version, numerical library, Maxwell codec and user setting.
It makes two narrowly pinned backend changes:

- NeuroConv0.7.0 uses a loaded NWB's source reader manager for dataset discovery,
  retaining the existing global-manager fallback for a fresh in-memory NWB.
  Reader-owned cached-schema classes can otherwise lose their electrode/model
  builder fields even when namespace version labels match.
- hdmf-zarr0.12.0 consolidates export metadata after writing cached specifications.
  Its original export consolidates before caching, leaving physical schema metadata
  absent from `.zmetadata`. Explicit `consolidate_metadata=false` is preserved.

`patch.lock.json` pins each version and complete original/patched module SHA-256.
The build refuses drift, changes only those two installed source files and their
installed RECORD entries, and compiles their bytecode. It does not replace a global
manager factory at runtime, skip source fields or install new dependencies. Original
dependency licenses and headers remain intact. The manager-only0.2.1 diagnostic
candidate is retained separately; it is not the complete backend candidate.

## Build

Build/pull the exact0.2.0 parent first, as documented beside its recipe. Its verified
local config ID is locked here; publication must freeze the corresponding registry
digest. From the repository root, choose a fresh evidence file outside Git:

```bash
python3 containers/nwb-backend/build.py --output /data/nwb-backend-build-01.json
```

The standard-library coordinator invokes Docker with networking disabled and uses
a content-named local alias for BuildKit's exact parent. It verifies the unchanged
parent-layer prefix, environment and `pipelineuser`. `image.json` records the local
ID, recipe hashes and verified scope; a registry digest remains pending publication.
Both maintained callers select it through the shared invocation image map.

## Focused verification

Run `scripts/modular/check_nwb_backend.py` in the exact candidate image with copied
native inputs and a fresh output directory. `--native` accepts both complete native
modeled HDF5 files; `--core` adds supported core HDF5/Zarr/legacy files; `--bad`
checks old unwritable graphs; `--stale` checks a retained pre-fix Zarr export.
Use `--expect-original-failure` in the0.2.0 parent to reproduce its original failure.

The checker configures and writes complete outputs, compares every authored/model
field and canonical reference, all retained raw values and new neural data/clocks,
and validates schema, physical/consolidated caches and unchanged input bytes.
It also exercises in-memory fallback, two independent source managers, explicit
unconsolidated export, stale-cache rejection and source/version patch-drift guards.
Core collection getters and HDMF column lists are compared in consistent in-memory
representations; no authored field is dropped and stored numeric types stay exact.
The snapshot explicitly traverses `Data.data`, which HDMF stores outside `.fields`,
so identifier, coordinate, text and object-reference payloads are included. Focused
corruption controls change actual Data values and canonical targets; schema, UUID
or builder checks alone do not establish their preservation.

One normal positive MaxOne pilot at source `cfb6178` completes the real reported
bundle verification, Ecephys export, Units append and final bundle publication.
Independent copied-output checks validate both schemas, the complete authored/model
graph and references, default LFP selection and exact clocks, and all 40,264 spike
times across 15 units. They account explicitly for the Units writer's existing
default Device addition and materialize empty projection collections consistently.

These focused checks do not replace real scientific producer acceptance, the full
zero/nonempty/layout/raw-enabled matrix, independent model/scientific validation,
or supported workflow/profile tests. Complete acceptance retains those separate
results and owners; the build-time `image.json` does not relabel them as its pilot.
