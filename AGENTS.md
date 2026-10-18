# MEA modular workflow maintenance

- Keep scientific defaults and CLI precedence in `pipeline/settings.nf`. Processes
  receive explicit invocation settings; do not read global scientific `params` from
  imported modules or use `include ... addParams` for per-instance settings.
- Keep process names stable unless changing and testing the backend selectors and
  trace consumers together. Test both Nextflow 26.04.6 and the NERSC 23.08.0-edge path.
- Report dependencies must be installed globally in the derived image. NRP changes
  `HOME` to `/tmp`, so build-time user-site installs disappear from its import path.
  Preserve the base scientific stack and restore its `pipelineuser` after installation.
- Nextflow 23 does not evaluate a closure supplied as the `overwrite` option.
  Keep default/true/false publication policies static and select through `saveAs`;
  test fresh and cached-resume behavior for all three on both supported runtimes.
- Use `trash` for local file deletion. Do not create a PR; the operator handles it.
- Keep fixtures and execution evidence outside Git. Tiny process probes establish
  binding/cache behavior, not scientific parity or NERSC/Shifter execution.
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
  Keep the corrected collector vendored at its recorded pin with its MIT license.
- A named report stage must stage the whole verified bundle, selecting its analyzer
  through `analyzer_subpath`, so lazy recording dependencies share its Docker mount.
  Original direct callers may continue staging only the analyzer.
- Preserve the real report capsule's empty statuses: zero analyzed units produce
  `no_detected_units`; a nonempty analyzer with no retained units produces
  `no_curated_units`. Both can legitimately feed burst `no_spikes`.
- Resolve a nested NWB recording's authored header from executable kwargs and the
  declared serialization base; a flat-directory scan misses staged raw sources.
  Preserve subject/session facts, and bind existing electrodes by channel plus
  group name before adding signals or units. Reusing a channel name with a new
  group can append duplicate rows or fail on a contiguous source electrode table.
  Keep shared NWB helpers explicit process inputs in both entry points.
