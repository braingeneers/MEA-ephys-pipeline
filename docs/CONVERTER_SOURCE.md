# Generated Maxwell converter source

[`pipeline/components/maxwell`](../pipeline/components/maxwell/README.md) contains
the complete maintained fresh converter, generated from Workflows commit
`0f3a980cab85192ad8e444be06be531fb3b7f2d9`. The exporter was introduced at `e7d1813`.
Its [manifest](../pipeline/components/maxwell/export-manifest.json) records original
paths/hashes, the exact generator, image pin, transformations and content identity.
Generated code retains upstream headers and observed notices; its source ownership
remains independent of the surrounding MEA code.

The native owner is `braingeneers-modular-maxwell-to-nwb` 0.1.39, common contracts
are 0.1.11, and the scientific image is the immutable 0.1.6 reference recorded in
`image.json`. Native outputs are verified artifact references after complete
publication. Source validation, conversion, channel-major repacking, compact
companion, LINDI, final validation and verification are included. Source replacement
and finalization are unsupported. See the generated README for standalone inputs,
Docker launch, runtime requirements and the development image build.

Use the component's Nextflow 26.04.6 config for its standalone conversion entry.
The existing five MEA stages continue to consume local units through their current
interface and retain Nextflow 23/NERSC compatibility. The separate
[`MEA_MAXWELL_CONVERSION` bridge](MAXWELL_CONVERSION.md) maps explicit recording/well
bindings to completed local units, retaining native and wrapper ownership plus all
declared projection/sibling dependencies. `main_conversion.nf` calls the same bridge
with its dedicated `nextflow_conversion.config` and explicit native `-lib` path.
Importing a complete converter config into old entry points would change their
runtime requirements.

To update, run the canonical `export_component.py` from the desired exact Workflows
commit, preparing a new directory outside both repositories. Its Python 3.11/Git
generator reads committed blobs, checks extraction identity and refuses existing
destinations. Regenerate twice and compare the manifest/file inventory. Review the
new source and generated diff, then commit the replacement tree. Do not hand-edit
generated scientific code or copy an unpinned live branch. The packaged verifier
checks bytes, modes and the complete file inventory against the generated manifest;
bind its content digest to the reviewed source revision as well.

This snapshot has 51 manifest entries (52 regular files), including the explicitly
staged `runtime/nwb_graph.py` guard, shared `runtime/owner_runtime.py` image guard
and its authoritative `runtime/owner-runtime-policy.json`. Its exact content digest is
`ecde1dc6156d33cdd3ab6411cb4732a0c16cdb2dda3657083bd6d06f59e25830`.
The native image preserves physical reference topology, while runtime identity v2
binds all verified scientific tool hashes and the extraction record. Both MEA
writers stage the same owning guard before export or source-copy/append. Historical
evidence keeps its original producer; a graph that cannot be written requires a
fresh conversion or explicitly validated immutable-source metadata refresh.

The preceding native 0.1.21 snapshot was checked with two deterministic exports, then five native and nine
portable bridge Nextflow tasks on the unchanged modeled Max One/routed Max Two
raw/projection pairs. All products retained verified compact companion, LINDI,
validation, publication and owner evidence. Both consumer export and copy/append
policies passed on all four fresh NWBs. An independent offline validator accepted
12 output copies with exact scientific/model/reference and fresh UUID comparisons,
and rejected 16 wrong-value/reference controls. Core HDF5 and Zarr writer controls,
both writer helper-staging failures, and 84-task alias graphs on each supported
Nextflow version also passed. Graph probes use test scientific capsules; they do
not establish scientific producer parity.

The preceding 0.1.22 source revision changes only the native manifest version in
this fresh export closure; its scientific tools, controllers, common runtime,
schema, libraries, defaults, lock and image retain the accepted bytes. New shared
adoption/reuse evidence helpers are outside the fresh entry point's dependency
closure. That export changed only its config version and generated
README/manifest provenance. Deterministic regeneration and bounded real caller
checks are separate from complete original-versus-exported scientific parity.

The historical 0.1.24 fresh component introduced a strict Jackson JSON decoder to preserve integer/float
encodings, signed zero and large integers at the owned input boundary. Its distinct version retained concurrent adoption changes outside that fresh
component; scientific tools, defaults, dependencies and image were unchanged at that update. The current component retains the decoder. See
[MAXWELL_CONVERSION.md](MAXWELL_CONVERSION.md) for supported numeric semantics
and the distinction from Nextflow's earlier parameter parsing.

The 0.1.30 import includes image 0.1.6's reviewed physical allocation repair:
repacking disables HDF5 file-space reuse so tiny compressed final chunks retain
physical channel-major order. Encoded chunk bytes/masks, the numerical copy loop,
all other scientific tools, defaults and dependency/plugin locks are unchanged.
The fresh controller shares maintained task/science/report helpers and additionally
checks each outcome's unit identity. Its image guard measures installed science
against the two recorded historical fingerprints; fresh exports include its policy
without copying reuse/adoption coordination. Common 0.1.9 adds opt-in publication
observations and independently reviewed upload-source identities while preserving
existing calls. Native entry points, process declarations and retry library are
byte-identical to the preceding 0.1.24 import.

Earlier 0.1.24 source/content provenance remains historical:
`beaa98304999034d28867ca218ff08ea0305820e`, digest
`79a0490983091635d65a5e86021cd34a33a93b5b56fa3a81c05a88ecc60fed6b`.
Import and interface regressions do not establish complete original/native/MEA
scientific graph acceptance at 0.1.30; that acceptance remains a separate gate.

The preceding 0.1.30 source remains historical at
`855cb3c17a85e7fd995e97118eec6dc348d3f6f6`, content digest
`ba6d10f19e0e288d41a2d8973e61ff696d35caf5067be0d88bfd7548d70c029f`.
The selected 0.1.39 export keeps the same scientific image/tools/defaults and
publication, allocation, schema, profile and retry policy. It adds strict frozen
native/common JSON claims and integer format versions, bounded route-helper
extraction, optional raw-source resolver plumbing and explicit fresh-only execution
control rejection. Its common package includes an unused shared discovery helper;
mixed native invocation, reuse and adoption coordination are excluded. The exporter
copies its maintained fresh templates directly, without rewriting Nextflow source.
Historical valid artifacts retain their original scientific producer identities.

The selected owner revision is local during integration validation. Make that exact
revision accessible from its maintained source repository before the upstream
handoff; runtime launches use the already-bundled source and pinned image. MEA source
and consumer image publication remain phase 05. Full original-versus-exported converter parity, refreshed pipeline-boundary
acceptance and GPU/cluster acceptance remain separate requirements. Earlier real
scientific producer acceptance keeps its actual source and image owners. Scientific fixtures/logs remain outside Git.
