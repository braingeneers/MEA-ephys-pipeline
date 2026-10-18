# Portable process modules

The direct entry point remains `pipeline/main_multi_backend.nf`. It imports the
sixteen process definitions in `pipeline/modules/` and preserves the original graph,
including all sorter branches and the currently disabled QC calls. This extraction
preserves scientific algorithms and defaults.

[Named scientific stages](SCIENTIFIC_STAGES.md) compose these same processes for
standalone or grouped execution with verified portable dependencies.

## Invocation interface

Every process takes a `task_settings` value map first, followed by its original
explicit path/value inputs. Each process has named outputs. Process names remain
stable so existing NERSC/NRP selectors and trace consumers continue to match.

`loadLaunchSettings(parameters, pipelineRoot)` in `pipeline/settings.nf` implements
the original CLI/environment/nested-JSON precedence. `processOptions(name, settings)`
returns only the owning task's arguments, worker options, capsule pin, image and
required publication paths, plus execution resources and the effective KS4 device.
Both functions are includable DSL2 functions. They
use invocation-local maps; imported processes never read scientific global params.

Callers stage checkout-owned capsule/helper directories as explicit `path` inputs.
An includable caller should resolve its own helper paths from `moduleDir`. Scientific
source already vendored in `capsules/` remains maintained there with its provenance;
changing a capsule pin alone does not replace that vendored code. The remaining
external capsules still check out their exact recorded commits.

The results collector is maintained locally at its original pin, with an explicit
`local_code` path input after settings. Both original and named callers stage the
same code. Its [documented correction](../capsules/results_collector/UPSTREAM.md)
preserves recording references to explicit pipeline data; it does not rename the
canonical `ecephys_session` directory into a session label. Collection no longer
performs a runtime Git checkout. Other still-external capsules retain their pins.

Curation is also maintained locally at its original Varda pin, with the same
explicit code input in both callers. Its [documented correction](../capsules/curation/UPSTREAM.md)
declares boolean QC masks for zero units while preserving the original nonempty
values, query, disabled model classifiers, labels and metadata. The original
runner/parameters/license are retained; curation no longer clones code at task time.

All explicit settings and capsule identities participate in task caching. The cache
check now invokes the actual preprocessing/KS4 modules with tiny argument-recording
capsules, covering changed and unchanged settings and pins. These probes establish
bindings/cache behavior, not scientific accuracy.

## Keyed producer ports

The eleven active processes also emit `unit_results`. Stage callers add a stable
`unit_id` to each task's settings and join independent producers by that key.
The original named result ports remain available to the direct graph.

Report generation receives `(recording_id, analyzer, recording_dependencies)`.
The dependency collection declares every referenced raw/prepared recording path;
direct callers broadcast it in stable path order to all analyzers. Named callers provide the complete
verified bundle, retaining their `analyzer_subpath`. Inputs are staged in separate
numbered directories under `capsule/recording-dependencies/` and are excluded from
report publication. An analyzer's serialized references alone do not declare the
files that the container needs.

`scripts/modular/check_report_dependencies.py` checks this port using real empty
analyzers/reports and two external binary dependencies with duplicate basenames.
Run it in the pinned driver with Docker and equal absolute source/evidence mounts;
give `--output` and a separate `--dependencies-root` outside that output. It reads
every recording frame, tests two-report fan-out and all publication/resume policies,
reverses dependency arrival order on resume, and requires omitted dependencies to
fail under a bounded task mount. `--legacy-runtime` selects the cached NERSC
Nextflow runtime for local Docker checks; it does not execute Shifter or sorting.

| Process | `unit_results` tuple |
| --- | --- |
| Job dispatch | `(task_settings, execution_record, results_directory, input_layout, duration_file)` |
| Report and burst | `(task_settings, execution_record, recording_id, results_directory, input_layout)` |
| Other active processes | `(task_settings, execution_record, results_directory, input_layout)` |

`execution_record` is assigned inside the per-task script context after invocation
settings bind. It contains the resolved container, requested CPU/memory/time/disk
and accelerator request/limit; it does not report observed utilization. Directly
reading `task` in a declarative output expression fails before task context exists.

`results_directory` is the declared `unit-results` relative symlink to the producer's
`capsule/results/`. `input_layout` is its declared `capsule/data/` directory. These
are explicit Nextflow path outputs for assembly, including the input layout's staged
references. The portable bundle helper dereferences them and uses their actual
serialization bases; a downstream stage must not guess a predecessor work directory.
Internal ports alone are not a verified portable bundle or stage-completion receipt.

Every publishing module restricts publication to `capsule/results/*`. The separate
internal alias avoids overlapping its directory with the original published child
paths: Nextflow deduplicates overlapping outputs before publication filtering.
See the [pinned Nextflow 23 publisher implementation](https://raw.githubusercontent.com/nextflow-io/nextflow/v23.08.0-edge/modules/nextflow/src/main/groovy/nextflow/processor/PublishDir.groovy).
Keep this alias and filter together, and test multiple report units so a whole-root
copy cannot overwrite another unit's published directory.

## Per-invocation resources

`process_resources` is an optional Nextflow parameter map keyed by canonical process
name. It can be supplied in configuration or a Nextflow params file. It is separate
from `--params_file`, which is the existing nested scientific capsule JSON.

```groovy
params.process_resources = [
    spikesort_kilosort4: [cpus: 4, memory: '8 GB', time: '60 min', disk: '16 GB']
]
```

The settings parser copies each process's resource map into its task settings.
NRP resolves those explicit values first, then the existing `nrp_resources` map by
the unqualified process name, then its original defaults. Named workflow scopes
therefore retain direct runtime overrides. Each KS4 task's effective CLI/JSON device
controls GPU admission, independently of other aliases' global launch parameters.
Keep shared thread exports, image pins, retry policy and NERSC separation intact.

## Images and backend behavior

Only report generation uses the derived
`braingeneers/mea-ephys-analysis:0.1.0` image. Build instructions are in
[the image directory](../containers/analysis/README.md). It contains the same base
scientific stack plus exactly the two spreadsheet dependency versions previously
installed by every report task. `--report_container_image` can select an equivalent
built image. Task execution performs no package installation.

NERSC launchers, Shifter configuration and the older Nextflow pin remain separate
from the opt-in NRP configuration. NRP retains its pinned base/KS4/NWB images,
thread limits, resources and GPU selector; its report selector uses the explicit
task image. Actual NERSC/Shifter execution requires an operator-side smoke test.

Publication keeps the original normal/resume defaults when `--publish_overwrite`
is unset, and explicit true/false behavior when supplied. Nextflow 23 clones named
option closures instead of evaluating the `overwrite` value. The modules therefore
use three static policies with mutually exclusive `saveAs` selections, preserving
the default engine behavior without adding a changing resume flag to task inputs.

## Checks

- `scripts/nrp/check_parameter_cache.py`: actual imported module inputs and seven
  setting/pin/cache cases, using stub scientific capsules.
- `scripts/nrp/check_compatibility.py local`: publication/resume and module-cache
  checks on the current local/containerized runtime, without submitting cluster work.
- `scripts/nrp/check_compatibility.py nersc`: pinned Java 17 / Nextflow 23.08.0-edge
  previews and shell-only execution; no Shifter task is run.
- `scripts/modular/check_report_runtime.py --output <result.json>`: compare installed
  packages with the exact base and perform an offline spreadsheet round trip with
  `HOME=/tmp`.
- `scripts/modular/check_fractional_lfp.py --fixture <generated-fixture> --output <new-directory>`:
  the original LFP regression on paired signed/unsigned fractional NWB inputs.
- `scripts/modular/check_nrp_profile.py --output <new-directory> --image <image>`:
  execute the imported report process with the NRP GPU profile and a local Docker
  overlay, proving its image selector follows the explicit settings. The report
  capsule is stubbed; this check does not submit a cluster task or exercise a GPU.
- `scripts/modular/check_unit_ports.py --output <new-directory> --mode docker|metadata`:
  actual imported process modules under two named aliases, with stub scientific
  capsules. Docker mode verifies task image execution and isolated publication;
  metadata mode observes CPU/CUDA allocation settings without CUDA computation.
  `--legacy-runtime <cache>` also exercises the pinned older parser with the NRP
  plugin removed for this local-only selector test. Its separate compatibility
  suite still previews the real NERSC configuration without running Shifter.
  The Docker overlay forwards the hook's thread variables through `envWhitelist`;
  the Kubernetes-native hook already runs inside its task container.
- `scripts/modular/check_collector_paths.py --original-code <pinned-code> --retained-run <real-run>
  --raw <raw-session> --bundle <analyzed-bundle> --output <new-directory>`:
  run the actual upstream/repaired collector on both original and portable real
  inputs. It compares recording fingerprints, all spikes, metric/extension files
  and curation masks; it identifies the original dangling path despite exit zero.
  It performs collection only, with no new sorting or analysis computation.

The runtime checks use `SOURCE_DIR` and `TEST_ROOT` for source/evidence locations,
as described in [README_NRP.md](../README_NRP.md). Evidence and generated acquisitions
belong outside Git. Use the producing scientific image for analyzer/extension checks.

The direct graph retains its original serialized recording references.
[Portable local bundles](PORTABLE_BUNDLES.md) package and verify their complete
dependencies for relocation. Independent stage entry points must use that tested
contract before claiming execution without the producing workspace.

The extraction checkpoint passed a real eleven-process CPU run on the generated
180-second, 32-channel fixture: 15 units and 40,440 spikes, with the original
analyzer checks and NWB validation passing. The NWB contains 32 electrodes and
225,000 LFP samples on eight channels at 1,250 Hz. An unchanged resume cached all
eleven tasks. This used default preprocessing and explicit no-motion **sorter**
settings, and does not establish GPU parity, NERSC execution or resource utilization
acceptance. The report image remains a locally verified build until its release
record contains a registry digest.
