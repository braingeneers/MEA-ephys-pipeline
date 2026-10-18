# Named Maxwell conversion

`pipeline/conversion.nf` exports
`MEA_MAXWELL_CONVERSION(recordings, settings, context)`. It calls the generated
native converter, verifies its completed publication through the native owner's
commit validator, materializes each selected unit in its own Nextflow task, then
commits the complete local unit set. Scientific conversion code is maintained in
the [generated component](CONVERTER_SOURCE.md).

This module requires Nextflow **26.04.6**, the pinned converter image and the
component's `MaxwellTaskRetry` library on the Nextflow classpath. For an imported
caller, use `nextflow run /path/to/caller.nf -lib
/path/to/pipeline/components/maxwell/lib ...`. A standalone acquisition entry and
its scoped deployment config are separate integration work. The existing
`main_stages.nf` and original direct workflow retain their five-stage NWB interface
and Nextflow 23/NERSC path; they do not import conversion or merge its full config.

## Inputs

`recordings` is a finite channel emitting **one complete nonempty array** of native
recording dictionaries. Each recording names an original `.raw.h5` through an
absolute POSIX path, canonical `file:///` URI or native-owned `s3://` source.
Keep uploaded well inventories, filters, unknown fields and per-unit
`nwb_metadata` projection references with their SHA-256 values. Generic native S3
reads stay with the converter; this local bridge adds no KS4 service/S3 adapter.

`settings` is the native scientific settings map. `{}` remains submitted `{}`;
effective defaults stay separately in provenance. Use the native `defaults.json`
as the maintained option definition. Companion creation follows the native
`create_linked_maxwell_companion` option; its declared sibling set is mandatory.
The bridge accepts no source replacement, finalization or deletion option.

`context` has these explicit fields:

- `workflow_version`: the owning MEA release, currently `0.2.9`. Native version
  `0.1.19` is read independently from its generated config, never replaced by the
  caller's MEA version or an enclosing Recipe revision.
- `output_uri`: an existing absolute POSIX directory or canonical local file URI.
  Use separate roots or distinct run/step identities for independent invocations.
- `run_id` and `step_id`: stable path-safe identifiers.
- `unit_bindings`: an exact binding for every selected recording URI/well, with
  `input_uri`, `unit_name` and `metadata.unit_id`. Max One uses `recording`; Max Two
  uses selected well names such as `well002`. Unit IDs are distinct path-safe
  strings; binding order and duplicate raw basenames do not establish identity.
- Optional `local_bind_paths`: existing files/directories for projections outside
  the original recording directories. These are explicit local task dependencies.
- Optional `runtime`: only the native transport/runtime options `s3_endpoint`,
  `aws_profile`, `public_reader_endpoint`, `minimum_disk_gib`,
  `require_k8s_aws_secret_name` and `k8s_aws_secret_name`.

Example binding, alongside a complete corresponding recording dictionary:

```json
{
  "input_uri": "file:///data/acquisition/recording.raw.h5",
  "unit_name": "well002",
  "metadata": {
    "unit_id": "experiment-a-well002",
    "operator_label": "culture A",
    "unknown_fields": {"nullable": null, "selected": false, "dose": 1.0}
  }
}
```

Binding metadata is preserved; `maxwell_conversion` is reserved for bridge
provenance. The native recording/well IDs remain separate and explicit. Unknown
recording fields and uploaded inventories are retained in each unit's complete
selection snapshot, even when a filter selects only one well.

## Completed outputs

- `units`: `(metadata, path)` tuples directly usable as the `units` input to
  `MEA_PREPROCESSING`. Each path is a verified, published local directory.
- `artifacts`: the native verified artifact envelope URI, emitted only after the
  bridge's final commit. Its contract and original owning identity are unchanged.
- `completion`: the bridge's create-only completion JSON URI, including verified
  unit receipts and an ordinary `units.json` file accepted by `--units_file`.

Native artifacts stay in the owning `derived/` namespace. Local copies publish
beneath `<output_uri>/bridge/<run_id>/<step_id>/units/<unit_id>/`. Completion and
`units.json` live beside `units/`. Existing different content is rejected. Exact
interrupted copies can confirm the same deterministic receipt; operational free
space/CPU/disk measurements remain separate task telemetry.

Each unit contains the unchanged NWB, original LINDI sidecar, validation sidecar,
and declared compact companion. `conversion/` retains the complete uploaded
selection, native envelope/stage/unit/evidence, and supplied projection bytes.
`conversion-unit.json` binds the complete file inventory and both owner versions.
The original LINDI URL retains its native published NWB identity; it is preserved
as native artifact evidence. Local NWB/companion execution uses the declared local
pair. The bridge's relocation check does not claim a new LINDI URL or full
original-vs-exported numerical parity.

The final commit verifies every local and published unit before any named output
emits. Partial materializations have no aggregate completion. Unit tasks never
delete shared raw sources or recording caches. Existing preprocessing packaging
copies the whole input directory into `data/ecephys_session`, and subsequent
bundles retain it, so missing siblings or provenance remain detectable.

For an independent relocated check, run `pipeline/helpers/maxwell_bridge.py verify
/path/to/unit` in the pinned converter image with its runtime/common helpers on
`PYTHONPATH`. It verifies native reference hashes, the exact local inventory,
modeled NWB/schema and companion link boundaries without opening original source
or publication paths. Use the emitted native artifact references for native
scientific provenance and the completion's unit receipts for local identities.

## Validation scope

The bounded implementation tests use real modeled Max One and routed Max Two
conversion, duplicate raw basenames, explicit reversed bindings, external
projection directories and typed unknown metadata. Guard checks use private
copies; they do not fabricate successful conversion. Consumer filter/model
compatibility, automatic acquisition entry/profile packaging, two-alias runtime
isolation, complete scientific parity and cluster/GPU/S3 service acceptance are
separate requirements.
