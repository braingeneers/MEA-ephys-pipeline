# Named scientific stages

`pipeline/stages.nf` exports five named DSL2 workflows over the original scientific
processes. `pipeline/main_stages.nf` is their standalone and grouped entry point.
The original `main_multi_backend.nf` retains its graph and alternative sorters.
The named sorting interface selects Kilosort4 explicitly.

| Export | Standalone `--stage` | Processes | Output kind |
| --- | --- | --- | --- |
| `MEA_PREPROCESSING` | `preprocessing` | Dispatch, preprocessing | `prepared` |
| `MEA_KILOSORT4` | `sorting` | Kilosort4 | `sorted` |
| `MEA_ANALYSIS` | `analysis` | Postprocessing, curation, visualization, collection | `analyzed` |
| `MEA_REPORTING` | `reporting` | Report generation, burst detection | `reported` |
| `MEA_FINAL_EXPORT` | `export` | Raw/LFP NWB, units export | `exported` |

`--stage all` connects these same exports. Final export waits for verified reports
and bursts for every expected recording. Deferring its raw/LFP branch changes
execution order and preserves the original algorithms. Source NWBs remain under
`data/ecephys_session/`; final derived exports are under `data/derived-nwb/`.
The [full entry](FULL_WORKFLOW.md) additionally composes native Maxwell conversion
before these same stages, using its separately scoped Nextflow26 runtime.

The complete maintained native Maxwell converter is available as a
[generated source component](CONVERTER_SOURCE.md). It has its own standalone
Nextflow 26 entry and verified native artifact outputs. The separate named
[`MEA_MAXWELL_CONVERSION` bridge](MAXWELL_CONVERSION.md) emits completed local units
for preprocessing. `main_conversion.nf` is its separate standalone entry with a
scoped config and explicit retry-library path. The existing five exports and
their Nextflow 23/NERSC path retain their interface.

## Local execution

Use the published [runtime images](../containers/README.md), install Docker and Nextflow,
and use an absolute POSIX output directory. Shared invocation settings select
base/KS4/NWB/report images; the Docker config sets two CPU threads and forwards thread
variables into task containers. Capsule `--n_jobs` is a separate explicit control.

```bash
nextflow -C pipeline/nextflow_stage_docker.config run pipeline/main_stages.nf \
  --stage all --unit_id acquisition-a --ecephys_path /data/nwb-session \
  --results_path /data/results --torch_device cpu \
  --params_file scripts/params_no_motion.json
```

Run `--stage preprocessing` with the same raw input options to publish a prepared
bundle, then launch sorting independently:

```bash
nextflow -C pipeline/nextflow_stage_docker.config run pipeline/main_stages.nf \
  --stage sorting --unit_id acquisition-a \
  --input_bundle /data/results/preprocessing/bundles/acquisition-a.prepared \
  --results_path /data/independent-sort --torch_device cpu \
  --params_file scripts/params_no_motion.json
```

The wrapper adds the stage directory. This example publishes
`/data/independent-sort/sorting/bundles/acquisition-a.sorted`. Continue with
`analysis`, `reporting`, then `export`, each receiving its predecessor kind.
Downstream calls retain predecessor unit metadata. Added metadata is allowed;
changing or dropping existing fields is rejected before scientific consumers run
and during assembly.

Multiple units use `--units_file /data/units.json`:

```json
[
  {"metadata":{"unit_id":"acquisition-a","inventory_key":"original-a"},"input":"/data/a/nwb"},
  {"metadata":{"unit_id":"acquisition-b","inventory_key":"original-b"},"input":"/data/b/nwb"}
]
```

For downstream stages, `input` is its complete predecessor bundle. Paths resolve
relative to Nextflow's launch directory. Unit IDs must be stable, path-safe and
distinct. Identical raw filenames/internal recording IDs in different units are
supported. Each finite stage input channel is collected once to validate unit
uniqueness before scheduling its scientific tasks.

The standalone unit/prior-bundle reader and scientific `--params_file` reader use
`pipeline/json_values.nf`. Original JSON integers retain arbitrary precision;
floating tokens use finite binary64, including negative zero. Duplicate keys,
trailing content, malformed values and floating overflow are rejected before
scientific tasks start. Unknown metadata keys and original input files are retained.

Imported callers can use `meaReadJson(file(...))` or `meaJson(originalText)` from
that module before constructing channels. Each stage eagerly snapshots unit
metadata independently for its invocation. Preserve the original files/strings
when exact numeric encodings matter: Nextflow's own parameter reader and Groovy
JsonSlurper can already have lost large integer values or negative zero before a
stage receives a map. The helper works on both supported Nextflow releases and
has no converter runtime/library dependency.

## Composition interface

Each export takes `(units, settings, context)` and emits `units`. Units are tuples
`(metadata, path)` with `metadata.unit_id`; unknown metadata fields are preserved.
Parse independent settings with
`loadLaunchSettings(explicitParameters, owningPipelineRoot)` in `settings.nf`.
Imported processes receive invocation-local maps. `process_resources` uses canonical
process names; `bundle_stage` controls packaging and verification resources.
See [process settings](MODULAR_PROCESSES.md).

```groovy
include { MEA_KILOSORT4 as SORT_A; MEA_KILOSORT4 as SORT_B } from './pipeline/stages'
// Inside a workflow, pass separate parsed settings and output destinations:
// ownerVersion is the literal version from pipeline/nextflow.config.
SORT_A(prepared_units, settings_a, [output_uri:'/data/a', workflow_version:ownerVersion, step_id:'a'])
SORT_B(prepared_units, settings_b, [output_uri:'/data/b', workflow_version:ownerVersion, step_id:'b'])
```

Context requires `output_uri` and the **owning** workflow version, with optional
`run_id` and `step_id`. It retains only these declared fields. A generated Recipe
revision must not replace scientific owner provenance. The standalone wrapper
reads the literal version from its owning `nextflow.config`.

Bundles publish under `<context.output_uri>/bundles/<unit-id>.<kind>`. Original
capsule publications use `<context.output_uri>/artifacts/<unit-id>/`. Capsule code
and helper code ports carry `meaCodeInput(file(...))` tuples, pairing a content
digest with each explicit module-relative path. Both maintained callers use the
same helper, including boundary/native-common and NWB writer code. Package
`pipeline/` (including `container_images.json`) and `capsules/` together. The NRP
development submission helpers additionally need `scripts/nrp/`.

## Boundary verification

Earlier `bundle_stage` and `verify_stage` boundaries use the pinned scientific
base runtime. Exported boundaries use the compatible base-derived
`container_images.export_verifier` runtime. Both use the
[portable bundle contract](PORTABLE_BUNDLES.md). They verify copied bytes and every
declared executable recording graph before downstream capsules can classify a load
failure as skipped work. Cumulative bundles retain raw NWBs, prepared binaries,
stage outputs and execution provenance. Size storage for their decoded payload and
temporary copies; this local contract does not perform S3 publication.

For units carrying `maxwell_conversion`, both boundary tasks also verify the native
conversion receipt, all published artifact bytes and retained provenance against
the exported contract/schema library. Receipt metadata must match the stage's
metadata exactly; new top-level invocation fields are allowed. Active NWB readers
must refer to that verified native NWB using their declared serialization bases.
Historical annotations are retained as provenance. The converter still performs
its full modeled NWB, schema and companion validation in its own runtime.

The helpers and exported `components/maxwell/components/common/python` tree are
explicit task inputs. Direct helper callers can supply `--common-code`; otherwise
the helper uses the workflow's exported tree. An invalid explicit tree never falls
back to an installed package. Generic units without native conversion claims keep
the portable bundle interface.

Serialization bases are explicit. The collector rewrites analyzer recording graphs
relative to its **publication** directory. Its maintained capsule preserves the
actual raw directory when pipeline data is explicit, while retaining the original
standalone/AIND naming without that argument. Assembly supplies the declared base
and retains the same lazy graph. The collector resolves source and target bases to
physical paths before computing relative references, including when a verified
input base is a symlink. A future publication directory need not exist or be created.
Producer-side copies are bound only after comparing
every byte, permitting JSON whitespace changes with unchanged values/types and
dictionary order. Timeseries layer order determines visualization rows and must
survive rebasing; only manifest hashes use canonical key sorting. A
matching basename alone never establishes dependency identity.

The preprocessing capsule defaults to a120-second minimum. A shorter recording
writes its original `Recording too short` evidence; the prepared-bundle guard
rejects that incomplete result and emits no successful prepared unit. Set
`preprocessing.min_preprocessing_duration` explicitly when scientifically intended.

Stages check the exact dispatch recording set and owned artifact families. Sorted
output requires a loadable sorting and terminal Kilosort log. Zero detections need
the shared capsule's exact exception/zero-detector evidence and typed status record.
Unrelated errors remain failures. Analysis requires a real analyzer, reporting
extensions, curation mask, classifier CSV, visualization and collected families.
The classifier CSV requires `decoder_label` and `decoder_probability` columns and
one row per analyzer unit, including units excluded by QC. Labels must be nonempty
and probabilities finite within `[0, 1]`; extra columns are retained. A genuine
zero-unit analyzer still requires the header with zero data rows. The shared
assembly/verification policy rejects missing or malformed classifier work before
publishing or releasing a stage input. Reporting preserves
`no_detected_units`, `no_curated_units` and burst `no_spikes`, and waits for every
expected report/burst recording. Export requires NWB structure and unit/spike counts
matching curation, plus a complete `metadata.derived_science` receipt. It binds every
stored group, dataset, physical attribute and typed Data buffer, including model
and ordered reference identities, raw/LFP clocks and samples, Units, metrics and
waveforms. HDF5 aliases and both Zarr metadata views are checked explicitly.
Full independent source-derived scientific NWB validation remains a separate
integration check.

Reported bundles also require `metadata.reported_science` schema1. Assembly binds
each reported unit and its complete float64 train to the analyzer's sorting and
sample clock, preserving the report's separate curation selection. It freezes the
ordered unit/train identities and every decoded typed report-summary and burst
field, including events, diagnostics and plot data. Burst unit counts follow the
capsule's nonempty-train selection; genuine `no_spikes` retains empty events in all
three families. Final export carries the verified report receipt unchanged.
Verification checks existing expectations and never creates a missing receipt.
Historical bundles without it keep their original reader and evidence; explicit
QA metadata authoring is separate from an ordinary workflow launch.

Final export reads an NWB source header from the declared recording graph, retaining
authored subject/session metadata, identifiers and existing electrode/device
bindings. Importing metadata does not enable raw export. Existing flat NWB inputs
still select their own output backend and retain their existing acquisition data.
The two NWB capsules share an explicitly staged [metadata helper](../capsules/nwb_common/README.md).
The helper also checks cached source schema versions and gives each read its own
namespace manager. The configured [NWB runtime](../containers/README.md) preserves
the numerical/SI stack while supporting the converter's core2.10 and
ndx-cell-culture1.0 files. Its reader layer and owning-manager/cache patches have
separate pinned recipes. Shared defaults now use their published immutable digests.
Full modeled writer and source Data verification remains distinct from
schema validity or a reader-only check.

Every complete bundle records ordered stage provenance, requested images/resources,
capsule pins/arguments, predecessor digest and validated outcomes. Failed assembly
can leave a partial directory, including a generic packing manifest. It lacks a
valid scientific receipt and cannot pass stage verification or emit successful
Nextflow output. Local bundle manifests are not S3 publication receipts.

## Validation and limits

`scripts/modular/check_stage_graph.py --output <fresh-directory>` executes all named
exports under two aliases and two units with identical raw basenames. Test-only
science stubs create real tiny SI/NWB payloads; report/burst capsules and all
production bundle verifiers remain real. Run inside the pinned Nextflow driver
with Docker access and equal absolute source/evidence mounts.
`--legacy-runtime <pinned-cache>` selects the older Java 17/Nextflow 23 runtime.

`check_stage_rejections.py --graph <graph-directory> --output <fresh-directory>`
rejects wrong units/kinds and incomplete scientific outcomes after resealing their
ordinary inventories/digests. `check_stage_boundaries.py` assembles each boundary
from retained real direct-run artifacts, without scientific recomputation.

`check_stage_entrypoints.py --graph <graph-directory> --rejections <rejection-directory>
--output <fresh-directory>` previews the grouped/downstream CLI, rejects duplicate
unit IDs and launches export against an incomplete reported bundle. Only its failed
verification task may run; both NWB processes must remain unscheduled. It supports
the same optional `--legacy-runtime` cache.

Fresh standalone/grouped and relocated scientific parity, actual NERSC/Shifter
execution and GPU/NRP science are separate acceptance checks. The Docker config
does not merge NERSC or Kubernetes configuration. S3 adapters and workflow-service
definitions remain independent of this portable scientific interface.

`check_nwb_source_metadata.py --source <input.nwb> --raw-output <derived-raw.nwb>
--units-output <derived-units.nwb> --baseline <previous-final.nwb> --output <check.json>`
runs in the pinned NWB worker. For the generated reference fixture, it compares
authored metadata, every source electrode column, every LFP sample/timestamp and
every unit column, with only newly generated per-unit UUIDs excluded. Add
`--flat-output <flat-input-export.nwb>` to check the existing flat-input path.
It also tests nested path bases, missing/ambiguous sources and multi-device channel
identity. Run the original isolated `scripts/nrp/validate_nwb.py` separately; do not
upgrade processing dependencies to obtain a compatible validation stack.

`check_stage_science.py --phase analyzers --direct <direct-run> --grouped <named-run>
--output <comparison.json>` compares a single reference recording with named unit
`reference`. Run it in the pinned report image, which adds Excel readers to the
base scientific stack. It reads every prepared/analyzer/published recording sample,
all analyzer extension arrays and PCA model states, report tables, bursts and
figures. `--phase nwb --source-nwb <input.nwb>` uses the isolated NWB reader stack
to compare both NWB producers' complete datasets and authored source metadata/
electrode rows. `--phase producers --source-nwb <input.nwb>` retains the complete
result/scratch file inventory (sizes and hashes) for every scientific process and compares all
non-Zarr producer payloads, including motion diagnostics and collector metadata.
Retained scratch includes the sorter recording/parameter/log JSON and LFP cache.
The original KS4 capsule deletes its raw Phy directory after loading its sorting;
those historical paths remain provenance, not emitted or retained artifacts.
Run all three phases: the inventory phase deliberately requires the logical Zarr
checks from the other two phases. It does not equate compression bytes with data.

This is a strict diagnostic check: differences and incomplete-check errors both
produce a nonzero exit and retained JSON. Generated IDs, named unit-column order,
known creation dates and extension execution times are recorded separately. Plot
sampling may still differ; the tool reports those differences. Sorter provenance
retains all parameters, scientific log text/counts/shapes and success/error evidence,
separating only known measured times, resources and relocation coordinates. Complete
serializer graphs, all recording samples/properties and source headers must be
checked together; filename normalization alone cannot prove source identity.
The collector's generated fallback description dates are separated only when the
supplied fixture directory lacks authored description/subject JSON. Its remaining
fallback metadata is compared as produced; it is not inferred authored NWB metadata.
A successful workflow or schema check alone does not establish exact
scientific parity. Excel comparison covers named sheets, values and formulas;
archive timestamps and workbook styling are outside that helper's logical-table
scope. Keep stochastic defaults and investigate numeric discrepancies
at their owner before accepting a modular implementation.

For a named-stage baseline, add `--direct-layout staged` to the science comparator.
It compares the same complete scientific scope; the left published analyzer then
uses the final exported bundle, just like the right. Trace views may join independent
stage executions through symlinked task directories; the comparator resolves those
declared task and published analyzer paths before loading their recording graphs.

`python scripts/modular/check_stage_isolation.py --session <raw-NWB-directory>
--params <complete-control.json> --output <fresh-directory> --forbid <original-path>
[--forbid <another-original-path>]` runs the five stages as separate real Nextflow
invocations. It physically copies each newly emitted bundle into the next stage
root, freezes the explicit runtime closure, makes copied inputs read-only in Docker,
rejects broader/original/symlink-escaping task mounts, and checks original-path
unavailability inside every actual task container. The host needs Docker access;
scientific execution uses the pinned driver/task images. Keep the output outside
all forbidden roots. Mount/launch success alone does not establish scientific parity;
run the complete comparisons and original validators over the resulting chain.
The tool currently targets the controlled single-recording CPU fixture and uses
the locally built report image digest; it is not an S3 or production launch wrapper.

`check_collector_bases.py --direct-task <collector-task>
--relocated-task <collector-task> --old-code <old-collector-code>
--output <fresh-directory>` replays the old and repaired collector CLIs against the
retained 15-unit CPU reference. Run it in the pinned base image with original task
trees read-only. It covers direct and symlinked raw bases plus a symlinked future
publication base that remains absent. It compares every recording sample/time,
gain/geometry property, spike, analyzer extension/PCA payload and curation mask, and
requires a missing recording dependency to fail. This collects retained science;
fresh independent stage execution remains a separate acceptance check.

`check_curation_masks.py --empty-task <curation-task>
--nonempty-task <curation-task> --original-code <pinned-curation-code>
--original-license <pinned-LICENSE> --output <fresh-directory>` runs four actual
old/fixed curation CLIs in the pinned base image, with original task trees read-only.
It reproduces the old float64 empty mask, requires the repaired bool `(0,)` mask,
compares all nonempty outputs and verifies input analyzers/metrics unchanged.
The source-delta guard retains the original runner, parameters and license. Both
entry points stage this maintained curation code explicitly; scientific queries,
labels and the report capsule's separate selection remain unchanged.

`check_bundle_layer_order.py --source <producer-preprocessedviz.json>
--portable <bundle/data/preprocessedviz.json> --output <fresh-directory>` checks
actual plot row order and rejects reordered dependency aliases while accepting
whitespace changes. `replay_render_inputs.py` replays the unchanged report or
visualization capsule in its original task image with test-only seeded waveform
and color-limit draws. Mount original task/data directories read-only. It records
draws, plotted arrays/styles, products and actual in-process thread settings.
Its report assertions target the 15-unit reference fixture; it does not establish
general fixture coverage or change production randomness. Compare its receipts
and products along with all three strict scientific comparison phases.

`check_science_corruption.py --phase arrays|nwb --run <named-run>
--output <fresh-directory> [--source-nwb <input.nwb>]` alters private copies of
real output. It requires rejection of a missing spike payload, changed metric,
sorter settings/counts/terminal evidence and an authored NWB identifier. Run arrays
in the report image and NWB in the isolated reader stack, with the whole source
tree available for shared metadata helpers. Never mount the original run writable.

For reproducible grid-convolution validation, supply the same explicit
`postprocessing.spike_locations.method_kwargs.prototype` vector in the complete
postprocessing JSON, alongside the desired random-spike/noise controls. SI 0.103.2's
node builder otherwise samples a separate 5,000-spike prototype with no seed; that
draw is absent from the extension's recorded parameters. Changing only the other
two seeds does not make spike locations or drift metrics reproducible.

Prepare the prototype with the pinned
`spikeinterface.sortingcomponents.tools.get_prototype_and_waveforms_from_peaks`
using the verified recording/peak selection, the actual location window and a
nonzero seed such as 42. Retain the returned selected peaks, waveforms, prototype
and input identities as validation evidence, then store `prototype.tolist()` in
the JSON. The pinned peak selector treats seed 0 as `None`; the explicit vector is
the durable control used by subsequent tasks.

The capsule converts only a supplied vector to finite float32 values, matching the
pinned sampled prototype dtype, and checks its one-dimensional sample count against
the recording rate and location window. Submitted JSON remains intact in provenance.
Omitting the prototype retains the original implicit sampling. Explicit `null`
retains SI's Gaussian fallback; it is distinct from omission. No scientific default,
threshold or localization method is changed. `check_prototype_parameters.py --output
<check.json>` exercises the real pinned API and rejects malformed vectors.

Motion diagnostics have a separate noise-estimation draw. For repeatable validation,
provide the complete preprocessing map with
`motion_correction.noise_levels = {"random_slices_kwargs": {"seed": 42}}`.
The capsule calls the pinned `get_noise_levels` API in raw units before motion
computation, which consumes its normal recording cache. It retains the actual
vector as `motion_<recording>/noise_levels.npy` and the sampling options in the
preprocessing process metadata. Omission keeps the original unseeded behavior.
This controls diagnostic peak detection even when motion application is disabled;
only `random_slices_kwargs` is accepted, preserving raw MAD estimation, the detection
threshold and motion algorithm.

### Bounded motion-displacement validation

`scripts/modular/check_motion_comparison.py` implements the reviewed comparison
for float64 motion displacement in micrometers. It requires matching dtype, shape
and finite masks, finite values throughout, absolute tolerance `1e-12` micrometers
and relative tolerance `1e-12` against the reference. Both arrays must also have
global scaled backward error at most eight float64 machine epsilons under the
same independently frozen motion equations. Recording, spike, model and other
scientific comparisons retain their exact policy. Estimation and production
settings are unchanged.

Supply time-by-window displacement `.npy` arrays, an independently constructed
float64 CSR matrix saved with SciPy's `save_npz`, and its float64 RHS vector `.npy`.
The equations stack each spatial window's time series (`displacement.T.ravel()`).
Freeze the equation inputs and identities independently of the measured candidate.
Run in the pinned scientific image:

```bash
python scripts/modular/check_motion_comparison.py --self-test
python scripts/modular/check_motion_comparison.py \
  --reference reference-displacement.npy --candidate candidate-displacement.npy \
  --system frozen-system.npz --rhs frozen-rhs.npy --output motion-comparison.json
```

The result distinguishes byte identity from numerical equivalence and records
complete array/matrix/RHS hashes, per-window differences and each array's backward
error. The tool reads saved products and leaves them unchanged. Its controls cover
out-of-tolerance displacement, bad candidate or reference backward error, malformed
shape/dtype and nonfinite or masked values.
