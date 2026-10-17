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
