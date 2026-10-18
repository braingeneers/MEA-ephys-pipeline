# Generated Maxwell converter source

[`pipeline/components/maxwell`](../pipeline/components/maxwell/README.md) contains
the complete maintained fresh converter, generated from Workflows commit
`01487885b17e8138f3b87c04bfe077f95bdb5ced`. The exporter was introduced at `e7d1813`.
Its [manifest](../pipeline/components/maxwell/export-manifest.json) records original
paths/hashes, the exact generator, image pin, transformations and content identity.
Generated code retains upstream headers and observed notices; its source ownership
remains independent of the surrounding MEA code.

The native owner is `braingeneers-modular-maxwell-to-nwb` 0.1.19, common contracts
are 0.1.8, and the scientific image is the immutable 0.1.3 reference recorded in
`image.json`. Native outputs are verified artifact references after complete
publication. Source validation, conversion, channel-major repacking, compact
companion, LINDI, final validation and verification are included. Source replacement
and finalization are unsupported. See the generated README for standalone inputs,
Docker launch, runtime requirements and the development image build.

Use the component's Nextflow 26.04.6 config for its standalone conversion entry.
The existing five MEA stages continue to consume local units through their current
interface and retain Nextflow 23/NERSC compatibility. This snapshot does not yet
implement `MEA_MAXWELL_CONVERSION` or an artifact-to-local-unit bridge. Conversion
integration must retain both native and wrapper ownership and provide explicit
stable unit/projection/sibling mappings. Importing the full converter config into
old entry points would change their runtime requirements.

To update, run the canonical `export_component.py` from the desired exact Workflows
commit, preparing a new directory outside both repositories. Its Python 3.11/Git
generator reads committed blobs, checks extraction identity and refuses existing
destinations. Regenerate twice and compare the manifest/file inventory. Review the
new source and generated diff, then commit the replacement tree. Do not hand-edit
generated scientific code or copy an unpinned live branch. The packaged verifier
checks bytes, modes and the complete file inventory against the generated manifest;
bind its content digest to the reviewed source revision as well.

The import was checked without a Workflows execution mount: two exact exports
matched, an offline independent image build passed its complete runtime identity,
and the standalone native entry completed five real Nextflow tasks on generated
modeled Max One/routed Max Two inputs with default submitted settings `{}`. Both
selected units produced valid NWB, compact companion, LINDI and validation siblings;
published products and original source/projection bytes reverified. These are tiny
converter/packaging checks. Full original-vs-exported scientific parity, consuming
codec/model runtimes, the named bridge and actual GPU/cluster acceptance remain
separate requirements. Scientific fixtures/logs remain outside Git.
