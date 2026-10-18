# Upstream capsule

Vendored from https://github.com/Varda006/aind-ephys-preprocessing at `827ee9b018247325e93f375ffe166c59dd64a19d`.
Only runtime code is included; the upstream license is retained.

Local change: only convert unsigned integer recordings before filtering.
Signed and floating-point inputs must retain their original representation.

Optional local extension: `motion_correction.noise_levels.random_slices_kwargs`
uses SpikeInterface's raw MAD noise cache to control motion diagnostic sampling.
Omission preserves upstream sampling. Explicit options retain the actual noise
vector in the motion results; see `docs/SCIENTIFIC_STAGES.md` in this repository.
