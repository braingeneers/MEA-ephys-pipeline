# Generated Maxwell converter source

[`pipeline/components/maxwell`](../pipeline/components/maxwell/README.md) contains
the complete maintained fresh converter, generated from Workflows commit
`7eedb8781050a955781541d587e4201365b7fcf3`. The exporter was introduced at `e7d1813`.
Its [manifest](../pipeline/components/maxwell/export-manifest.json) records original
paths/hashes, the exact generator, image pin, transformations and content identity.
Generated code retains upstream headers and observed notices; its source ownership
remains independent of the surrounding MEA code.

The native owner is `braingeneers-modular-maxwell-to-nwb` 0.1.23, common contracts
are 0.1.8, and the scientific image is the immutable 0.1.5 reference recorded in
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

This snapshot has 48 manifest entries (49 regular files), including the explicitly
staged `runtime/nwb_graph.py` guard. Its exact content digest is
`3ddd80ec3b3346338dc23c778ea35ed5072e9c28c58c29f5ee050af9d6e27640`.
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

The current 0.1.23 source adds a strict Jackson JSON decoder to preserve integer/float
encodings, signed zero and large integers at the owned input boundary. Scientific
tools, defaults, dependencies and the scientific image are unchanged. See
[MAXWELL_CONVERSION.md](MAXWELL_CONVERSION.md) for supported numeric semantics
and the distinction from Nextflow's earlier parameter parsing.

The native shared dependency is publicly pinned. MEA source and consumer image
publication remain phase 05. Full original-versus-exported converter parity, refreshed pipeline-boundary
acceptance and GPU/cluster acceptance remain separate requirements. Earlier real
scientific producer acceptance keeps its actual source and image owners. Scientific fixtures/logs remain outside Git.
