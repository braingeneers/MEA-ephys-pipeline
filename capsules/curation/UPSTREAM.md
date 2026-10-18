# Upstream capsule

Vendored runtime from https://github.com/Varda006/aind-ephys-curation at
`c8a994bae531ad75ec0fdcbe6c58959a3e872d37`, the existing `CURATION` pin.
The original Allen Institute MIT license is retained. This pin includes Varda's
disabled model classifiers and labels all units as SUA while recording QC
separately; those decisions, the curation query/defaults, metadata, runner and
parameters remain unchanged.

Local change: declare the QC mask's NumPy dtype as bool. An empty comprehension
otherwise produces a float64 array, breaking the downstream boolean-mask contract
after a valid zero-detection sort. Nonempty masks retain their original values and
dtype. The stage verifier remains strict.
Four upstream trailing-whitespace lines are normalized.
