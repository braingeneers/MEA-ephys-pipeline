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
/path/to/pipeline/components/maxwell/lib ...`. Keep `-lib` among the `run` options,
as described in the [Nextflow CLI reference](https://docs.seqera.io/nextflow/reference/cli/run).
The existing
`main_stages.nf` and original direct workflow retain their five-stage NWB interface
and Nextflow 23/NERSC path; they do not import conversion or merge its full config.

## Standalone conversion

Create the output directory and save a params file such as `/data/conversion.json`:

```json
{
  "recordings_file": "/data/recordings.json",
  "unit_bindings_file": "/data/unit-bindings.json",
  "settings": {},
  "output_uri": "/data/results",
  "run_id": "experiment-a-conversion",
  "step_id": "convert",
  "local_bind_paths": ["/data/projections"]
}
```

```bash
NXF_SYNTAX_PARSER=v1 nextflow -C pipeline/nextflow_conversion.config \
  run pipeline/main_conversion.nf -lib pipeline/components/maxwell/lib \
  -profile docker -params-file /data/conversion.json
```

`recordings_file` and `unit_bindings_file` contain the complete arrays described
below. Alternatively, pass `recordings` and `unit_bindings` as JSON arrays. Use
`settings_file` for a scientific settings object in its own file; supplying both
a nonempty `settings` map and `settings_file` is rejected. No scientific defaults
are inserted into submitted `{}`. Pin `run_id` when referring to this publication
later; an omitted ID is generated once for that invocation.

`nextflow_conversion.config` includes only the exported deployment configuration.
Its `docker` and `k8s` profiles retain native image/resource/retry policy in the
process definitions. It does not include the old KS4 `task_settings` selectors.
Kubernetes uses Jobs; local file inputs/output paths must be visible on the shared
workspace with the same absolute paths. Select namespace, service account and PVC
explicitly for that deployment. NERSC settings remain in their existing config.

The printed `BRIDGE_COMPLETION` names a JSON document with a `units_file` reference.
Its ordinary local `units.json` can feed `main_stages.nf --stage all --units_file
/path/to/units.json` with the separate KS4 configuration. The consumer-image
codec/model validation and complete scientific acceptance remain distinct from
conversion-entry validation.

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

- `workflow_version`: the owning MEA release, currently `0.2.21`. Native version
  `0.1.30` is read independently from its generated config, never replaced by the
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

Imported callers can use `conversionOwnerVersion()` from `pipeline/conversion.nf`
for the actual owning version rather than an outer workflow/Recipe version.
Decode original JSON with `conversionJson(text)` before creating imported value
channels. The owning decoder uses Jackson bundled with Nextflow 26.04.6: JSON
integers retain arbitrary precision; floating tokens use finite IEEE binary64,
including signed zero and exponent-form floats. Duplicate keys, trailing content,
nonstandard values and overflow to infinity are rejected. The standalone entry
uses this same decoder for `*_file` inputs and JSON strings. Each invocation then
takes an eager independent snapshot before fan-out, retaining nested unknown
metadata, scalar types and map order while isolating concurrent aliases.

For exact original numeric fidelity, use those files/strings or already-correctly
typed channels. Nextflow's own `-params-file` object reader and Groovy JsonSlurper
can lose signed zero, turn exponent-form floats into integer output, and overflow
large integers before this workflow receives them. A snapshot cannot restore that
information. Missing retry-library loading or a different Nextflow version is
rejected before native tasks schedule.

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
interrupted copies can confirm the same deterministic receipt. Free-space
observations and disk requests remain separate task telemetry; CPU/memory requests
remain in the Nextflow trace.

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
compatibility, complete scientific parity and cluster/GPU/S3 service acceptance are
separate requirements.

Bridge plans and completed result JSON use this same decoder. The emitted
Nextflow unit tuple preserves original metadata types and signs; validating the
persisted JSON alone does not prove that downstream port value.
