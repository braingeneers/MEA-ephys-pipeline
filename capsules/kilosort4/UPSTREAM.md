# Upstream capsule

Vendored runtime code from
https://github.com/AllenNeuralDynamics/aind-ephys-spikesort-kilosort4 at
`dbe0c9683433f0d10057b9db800c62654ee1abf5`, the existing `SPIKESORT_KS4` pin.
The upstream MIT license is retained.

Local change: recognize Kilosort's explicit zero-spike detector outcome and save
a loadable zero-unit sorting, status metadata and the original detector log.
Both the exact terminal exception and zero-detection log evidence are required;
other errors retain the original failure behavior. Detection thresholds and
nonempty sorting behavior are unchanged.

The unchanged `detected_no_spikes` function lives in `code/outcomes.py` so stage
validation and the capsule use one maintained classifier.

The launch wrapper also prepares Kilosort4.0.38's exact predefined numerical
template before importing the sorter, using its existing
`KILOSORT_LOCAL_DOWNLOADS_PATH` environment variable. `code/predefined_template.json`
retains the official OSF version1 input from
https://osf.io/download/6807fb5958b763aae139aa60/ as text-encoded bytes:3432 bytes,
SHA256 `cae1c96f8f4150be0a39627515750b70c4bc3548177cf487ae3c013f1ca6abd8`.
The downloaded bytes match OSF's independent file metadata at
https://api.osf.io/v2/files/6807fb5958b763aae139aa60/.
`code/prepare_templates.py` verifies the payload and task-local cache before the
scientific command. Missing/corrupt asset data or corrupt existing cache contents
fail before sorting. This removes the live template-download dependency while
preserving predefined template values, learned-template behavior, sorter options,
worker image and the scientific `run_capsule.py` implementation.
