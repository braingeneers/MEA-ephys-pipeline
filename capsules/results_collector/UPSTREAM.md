# Upstream capsule

Vendored runtime from
https://github.com/AllenNeuralDynamics/aind-ephys-results-collector at
`ffc239b55b3bfbc45408e38de5f95a4284f9f8bf`, the existing `RESULTS_COLLECTOR` pin.
The upstream MIT license is retained.

Local change: when `--pipeline-data-path` explicitly identifies the source data,
preserve remapped recording references instead of globally renaming
`ecephys_session` to the session name. A portable bundle legitimately uses that
directory; renaming it creates a nonexistent source path while the collector exits
successfully. Standalone/AIND collection without an explicit pipeline data path
retains its original session-name mapping. Scientific arrays, metrics, curation,
metadata collection and output families are unchanged.
Four upstream trailing-whitespace lines are normalized.

Serialization source and target bases are resolved to their physical coordinates
before computing relative paths. A symlinked `verified/data/ecephys_session` base
otherwise produces paths whose `..` traversal escapes the actual raw directory.
Future publication directories may still be absent; resolution does not require
their creation. This retains the original recording graphs and payloads.
