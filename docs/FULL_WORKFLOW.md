# Complete local Maxwell or NWB workflow

`pipeline/main_full.nf` connects the maintained named conversion, preprocessing,
Kilosort4, analysis, reporting and final-export workflows in one Nextflow run.
It requires Nextflow26.04.6 and the converter's explicit library path. The original
`main_multi_backend.nf` and NWB-only `main_stages.nf` retain their Nextflow23/NERSC
interfaces. Use those entries for other sorters or existing NERSC launchers.

Scientific settings, images and resources come from the same
[process settings](MODULAR_PROCESSES.md) as independent stages. Conversion has a
separate `conversion_settings` object; omitted defaults stay with its native owner.
The local full config applies MEA resource/image/thread settings only to MEA
processes, preserving converter task resources and its retry library. It does not
merge the NERSC or NRP profiles.

## Start from an NWB session

```bash
NXF_SYNTAX_PARSER=v1 nextflow -C pipeline/nextflow_full_docker.config \
  run pipeline/main_full.nf -lib pipeline/components/maxwell/lib \
  --input_mode nwb --unit_id acquisition-a --ecephys_path /data/nwb-session \
  --results_path /data/results --torch_device cpu
```

Use `--torch_device cuda` for an explicitly selected CUDA device. CPU examples
are useful for small controls; they do not establish GPU execution. Omit
`torch_device` to retain the shared capsule's existing automatic device setting.
Pass scientific overrides with `--params_file science.json`, separately from
Nextflow's `-params-file launch.json`.

For several sessions, use `--units_file units.json` with the same complete
`input`/`metadata` rows as [independent stages](SCIENTIFIC_STAGES.md). A completed
converter bridge already supplies this file: read `units_file.uri` from its
completion, then use `input_mode=nwb` and that file. This preserves the verified
native receipts, projection/inventory metadata and sibling files without
reconverting. A directory alone does not carry its separate unit metadata.

## Start from raw Maxwell recordings

Prepare the complete `recordings.json` and exact `unit-bindings.json` arrays from
[the conversion interface](MAXWELL_CONVERSION.md), including selected MaxTwo wells,
metadata projection references/digests and uploaded inventories. Create the local
conversion publication directory:

```bash
mkdir -p /data/results/conversion
```

Save `launch.json`:

```json
{
  "input_mode": "maxwell",
  "recordings_file": "/data/recordings.json",
  "unit_bindings_file": "/data/unit-bindings.json",
  "conversion_settings": {},
  "local_bind_paths": ["/data/projections"],
  "results_path": "/data/results",
  "run_id": "acquisition-a",
  "step_id": "full",
  "torch_device": "cpu"
}
```

```bash
NXF_SYNTAX_PARSER=v1 nextflow -C pipeline/nextflow_full_docker.config \
  run pipeline/main_full.nf -lib pipeline/components/maxwell/lib \
  -params-file /data/launch.json
```

`conversion_settings_file` accepts an independently authored JSON object. It is
mutually exclusive with a nonempty `conversion_settings`. Keep original JSON in
these files when numeric type/sign fidelity matters. Conversion settings and
runtime keys retain the native interface's allowlist; source replacement and
finalization remain unsupported. The full portable entry publishes to local POSIX
directories. Native S3 acquisition reads retain the converter's own transport;
durable S3 transport for the five later stages belongs to a separate adapter.

Conversion validates and publishes the complete native artifact set, materializes
all selected local units, and commits their completion before preprocessing sees
any unit. No task associates a well by arrival order or basename. The original
converted NWB and declared companion/LINDI/validation/projection evidence remain
in each cumulative bundle. Original LINDI URLs retain their native publication
identity; generic LINDI0.4.6 file-URI/compressed-padding limitations remain distinct
from native range verification.

## Outputs and imports

Under `results_path`, conversion owns `conversion/`; the later stages own
`preprocessing/`, `sorting/`, `analysis/`, `reporting/` and `export/`. Each scientific
stage emits its ordinary verified bundle. The final bundle is
`export/bundles/<unit-id>.exported`. Raw and derived NWBs stay separate. Final NWB
writing waits for completed reporting and bursts; this changes the original
independent export branch's order, while retaining its scientific producers.

The full entry prints `NATIVE_ARTIFACTS`, `BRIDGE_COMPLETION` for raw input and
`Verified full unit` only after the corresponding maintained outputs emit. Failed
verification never becomes a successful unit. See [bundle contracts](PORTABLE_BUNDLES.md)
for dependencies, storage sizing and empty-result semantics.

Imported callers can use `MEA_FULL_NWB(units, settings, context)` or
`MEA_FULL_MAXWELL(recordings, conversion_settings, settings, context)` from
`pipeline/full.nf`. The first accepts the existing local unit tuples. The second
accepts the complete conversion selection and requires its ordinary context
bindings/runtime, plus an existing `<context.output_uri>/conversion` directory.
Each stage gets its own output directory and derived step ID. Preserve the actual
owning MEA version in `context.workflow_version`; native ownership remains separate.

The component source is already included, with [repeatable update provenance](CONVERTER_SOURCE.md).
Package `pipeline/` and `capsules/` together, including image/pin metadata and the
native library. No local Workflows-repository checkout is needed at runtime.

For Maxwell input, optional `native_output_uri` publishes the native conversion
artifacts to an explicit S3 prefix while the bridge and later stages retain local
directories. Supply the native runtime's S3 and public-reader endpoints as described
in [conversion context](MAXWELL_CONVERSION.md#inputs). This option is rejected for
NWB input. Durable transport for the later MEA stages belongs to their S3 adapter.
