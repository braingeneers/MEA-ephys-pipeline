# MEA modular workflow maintenance

- Keep scientific defaults and CLI precedence in `pipeline/settings.nf`. Processes
  receive explicit invocation settings; do not read global scientific `params` from
  imported modules or use `include ... addParams` for per-instance settings.
- Treat `pipeline/components/maxwell` as generated source. Fix its maintained
  upstream owner and regenerate from an exact commit; do not hand-edit a second
  converter. Preserve its native version/image ownership separately from MEA's
  wrapper version and keep its Nextflow 26 config scoped to conversion callers.
- Nextflow `collect()` flattens tuple rows by default. Use `collect(flat:false)`
  before sorting keyed producer tuples for a completion barrier; otherwise metadata
  and paths become unrelated list entries after the producers finish.
- Derive converter phase and sibling requirements from its effective options.
  Disabling compact-companion creation legitimately omits that phase/artifact;
  retain every remaining native validation and completion check.
- Groovy JsonSlurper LazyMap values can lose fields when aliases serialize the
  same tree concurrently. Materialize an eager per-invocation JSON copy under the
  shared input's lock before fan-out, preserving numeric encodings and unknown keys.
- Keep process names stable unless changing and testing the backend selectors and
  trace consumers together. Test both Nextflow 26.04.6 and the NERSC 23.08.0-edge path.
- Report dependencies must be installed globally in the derived image. NRP changes
  `HOME` to `/tmp`, so build-time user-site installs disappear from its import path.
  Preserve the base scientific stack and restore its `pipelineuser` after installation.
- BuildKit parses a raw `sha256:<config-id>` in FROM as a registry image name.
  For an unpublished immutable parent, use a content-named local alias, verify its
  ID before/after the build and require the child's exact parent-layer prefix.
- Nextflow 23 does not evaluate a closure supplied as the `overwrite` option.
  Keep default/true/false publication policies static and select through `saveAs`;
  test fresh and cached-resume behavior for all three on both supported runtimes.
- Use `trash` for local file deletion. Do not create a PR; the operator handles it.
- Keep fixtures and execution evidence outside Git. Tiny process probes establish
  binding/cache behavior, not scientific parity or NERSC/Shifter execution.
- Give out-of-order alias probes enough driver CPU capacity to run both aliases
  concurrently (12 CPUs for the full port probe). A four-CPU local scheduler can
  queue the second two-CPU unit until the first finishes; a timing assertion then
  fails despite correct inputs and all task exits0. Preserve the failed trace and
  rerun in a fresh root with adequate capacity, without weakening the assertion.
- The original direct caller fans out each dispatch result as a preprocessing
  job. When adapting named-stage stubs to that caller, keep their extra diagnostic
  receipt outside declared dispatch results. A diagnostic JSON is not a job.
- Resolve shared consumer image defaults in `pipeline/settings.nf` from
  `pipeline/container_images.json`. Preserve per-invocation overrides in process
  and boundary tasks; Docker/NRP selectors must follow explicit task settings.
  Keep legacy report-image precedence and NERSC's independent profile.
- A loaded Zarr analyzer can silently have no recording. Portable stage wrappers
  must verify the bundle and read recording samples before invoking capsules that
  classify load errors as skipped work. Missing dependencies are never zero spikes.
- Keep executable references distinct from historical provenance. Rebase the exact
  lazy recording graph, including its raw NWB and binary dependencies, using explicit
  serialization bases. Do not substitute a binary cache or infer a producing task
  directory from a basename. Preserve the capsule's JSON and pickle fallbacks.
- Scientific fingerprints must retain Pandas nullable values distinctly from None,
  NaN and infinity. Template metrics can contain `pd.NA`; do not drop those values
  or change scientific tables to make packaging JSON-serializable.
- Build execution metadata in the process's per-task script context. `task` is not
  available while a declarative output expression is being defined. Emit the scoped
  variable and keep requested resources distinct from observed utilization.
- Internal whole-result ports must use the separate `unit-results` alias and every
  publisher must filter `capsule/results/*`. Nextflow collapses overlapping parent
  and child outputs before publication filtering; publishing a parent directory can
  overwrite another recording's results and publish staged scientific inputs.
- NRP's beforeScript runs inside Kubernetes task containers. A local Docker probe
  needs `docker.envWhitelist` for the exported thread variables, because its hook
  runs outside the container. Do not use an unadapted Docker probe as evidence of
  in-container thread limits. Scientific capsule `--n-jobs` is a separate control.
- The collector serializes analyzer recordings relative to its declared future
  publication path. Use that explicit base for source loading/fingerprinting, then
  rebase into the bundle; do not assume the collector task's analyzer directory.
  When an explicit pipeline data path is supplied, preserve its raw directory;
  global `ecephys_session` replacement can exit zero with a dangling recording.
  Resolve both source and target serialization bases before computing relative
  paths: a physical target with a logical symlink base gives invalid `..` traversal.
  Future publication bases may be absent; resolving them must not create them.
  Keep the corrected collector vendored at its recorded pin with its MIT license.
- A named report stage must stage the whole verified bundle, selecting its analyzer
  through `analyzer_subpath`, so lazy recording dependencies share its Docker mount.
  An analyzer-only direct report port can hide missing recording dependencies when
  Docker infers a broad common mount. Test source/work in one bounded root and
  supply raw/prepared dependencies explicitly; changing archive placement is not
  the repair. Retain the original lazy recording instead of substituting a cache.
  The report input tuple is `(recording_id, analyzer, recording_dependencies)`;
  keep dependencies attached to their recording, stage duplicate basenames in
  numbered directories, sort dependency inputs for stable caching after out-of-order
  producers, and preserve the declared-value broadcast to fan-out.
- Preserve the real report capsule's empty statuses: zero analyzed units produce
  `no_detected_units`; a nonempty analyzer with no retained units produces
  `no_curated_units`. Both can legitimately feed burst `no_spikes`.
- Declare curation masks with `dtype=bool`, including zero units. NumPy infers
  float64 from an empty comprehension even when nonempty values are boolean.
  Repair this at the pinned producer; retain strict stage mask validation and
  preserve scientific queries, labels and the separate report selection.
- Resolve a nested NWB recording's authored header from executable kwargs and the
  declared serialization base; a flat-directory scan misses staged raw sources.
  Preserve subject/session facts, and bind existing electrodes by channel plus
  group name before adding signals or units. Reusing a channel name with a new
  group can append duplicate rows or fail on a contiguous source electrode table.
  Keep shared NWB helpers explicit process inputs in both entry points.
- Inspect cached namespace versions before merging them into an invocation-owned
  NWB type map; installed schemas can otherwise silently replace incompatible
  cached versions. Test the concrete HDF5 and Zarr constructors, legacy core files
  and byte-string version headers. Import success alone does not prove backend
  compatibility, modeled export/append or full scientific producer acceptance.
- Backend dataset discovery must use a loaded NWB's owning reader manager;
  matching namespace versions do not make private cached-schema classes compatible
  with a fresh global builder. Keep the in-memory fallback and never replace a
  global manager factory in maintained task code. Pinned compatibility patches
  belong in the workflow-owned image recipe with exact source/version guards.
  Zarr export must finalize consolidated metadata after caching specifications;
  verify both metadata views and preserve an explicit unconsolidated request.
- HDMF `Data.data` is separate from container `.fields`. Metadata verification
  must traverse both, including numeric/identifier/string columns and object
  reference arrays. Schema validity, UUIDs and builder presence do not prove
  payload values; retain real coordinate/ID/reference corruption controls.
- Stage the owning NWB graph guard explicitly into both writer modules and both
  entry points. Preflight sources before export or any destination copy; old
  expanded-reference graphs may read successfully but cannot be written. Retain
  private namespace maps and permit valid reference links, including core aliases.
  Zarr export controls must use the capsule's `link_data=False` policy, and each
  fixture file needs a fresh NWB container instance.
- Native control generation retains the zero-centered float signal and biased ADC
  acquisition as distinct identities; preserve quantization/calibration and never
  claim generated ground truth as a KS4 outcome. SI injected-template recordings
  are not JSON serializable, so retain the complete reference signal and recipe.
  Neo numeric channel strings and NWB `chN elecM` names map through the explicit
  channel/electrode table; compare that mapping instead of assuming identical labels.
- SI 0.103.2's localization node builder silently samples a 5,000-spike prototype
  separately from the analyzer's random-spike/noise extensions. For exact validation,
  pass one verified explicit prototype through the existing method kwargs; convert
  its JSON vector only for compute and preserve submitted JSON provenance. Missing
  and null prototypes have different existing meanings. Keep stochastic defaults.
  The pinned peak selector treats seed 0 as None; retain a working nonzero seed and
  the actual selected peaks/vector when preparing a reproducible control.
- Startup thread probes do not establish final library threading. The maintained
  postprocessing capsule sets OPENBLAS_NUM_THREADS=1 before importing NumPy, while
  scientific worker count remains separately controlled. Preserve that behavior
  and match it in capsule replays rather than inferring it from a startup probe.
- Preprocessing motion diagnostics have their own unseeded noise draw, even when
  motion is not applied. Use the optional motion noise sampling map for controlled
  validation and retain the actual raw noise vector. Compare every saved motion
  peak/location/displacement array; equal prepared samples do not cover diagnostics.
- Preserve JSON dictionary order when rebasing capsule serializations. The
  visualization capsule iterates timeseries layers to choose plot rows; sorting
  keys reverses raw/highpass and highpass/CMR rows. Keep canonical manifest hashes
  separate, and reject dependency aliases whose JSON key order differs.
- Kilosort's total-runtime clock pads hours/minutes but uses unpadded rounded
  seconds, including possible 60. Validation may separate that measured duration;
  retain scientific counts/shapes, parameters and terminal evidence in its log.
- Mount validation hooks explicitly for every Docker task. A capsule cloned inside
  its work directory can infer only that mount; an unavailable BASH_ENV hook may silently
  produce no receipt. Count actual in-container evidence separately from bounded
  mount requests, including after an unrelated workflow failure.
