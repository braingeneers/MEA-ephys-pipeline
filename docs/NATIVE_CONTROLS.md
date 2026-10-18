# Native modeled validation controls

`scripts/modular/prepare_native_controls.py` creates reproducible Max One and
routed Max Two input controls for scientific writer validation. This is a local
validation tool; it does not change workflow defaults or select production images.
It requires the pinned SI0.103.2 codec-enabled base stack, complete authored model
projections for each layout, and a recordings template using the conversion
interface documented in [MAXWELL_CONVERSION.md](MAXWELL_CONVERSION.md).

The fixed control has180seconds,20kHz,32channels,20generated units and seed42.
The source generator's float32 signal and generated spike vector are retained.
ADC encoding rounds `generator_uV / 6.3 + 512` to uint16, rejecting any value
outside0–1023 rather than clipping it. The native recording's physical calibration
is6.3uV/count with zero offset, so its DC baseline is512counts. The original
zero-centered generator and the encoded acquisition are distinct scientific
identities; their hashes and bounded quantization error are recorded separately.
Generated spikes are ground truth input, never an observed KS4 outcome.

All raw data, geometry, source timing, generation/package identity and complete
projections are written into one fresh external directory. Max Two retains both
well000 and well002 while the recording template selects well002. Real hard links
preserve the expected data-store/well/recording aliases. Original projection
fields and numeric encodings are preserved except the explicitly recorded new
control identities and truthful180second duration. The fixed acquisition start is
`2026-01-01T00:00:00+00:00`; pass that explicit value as the converter's
`settings.session_start_time` for these controls.

The output contains `recordings.json`, `generation.json`, raw files and projections
under `maxone/` and `maxtwo_routed/`, `geometry-um.npy`, `generator-traces.f32`,
`encoded-adc.u16`, `generated-spikes.npy` and the saved `ground_truth/` sorting.
The float/ADC reference signals alone use691.2MB; reserve additional space for
three raw well datasets, converted artifacts and execution/verification copies.
No existing destination is accepted.

## Local development example

The base codec candidate is unpublished until the release phase. Use the exact
locally verified image ID below, or build and verify the pinned equivalent using
[the codec build instructions](../containers/maxwell-codec/README.md). Supply your
own complete projection/recordings templates in the external `/validation` mount.
The example creates `/validation/native-controls` only if it is absent.

```bash
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --cpus 2 --memory 8g -e HOME=/tmp \
  -e OMP_NUM_THREADS=2 -e OPENBLAS_NUM_THREADS=2 \
  -v "$PWD:/source:ro" -v /absolute/validation:/validation \
  sha256:f09087aa24b5d3cc9ee09dd9c02ea180eeadb1be45ae043d99a32ee3c8bbc9d7 \
  python /source/scripts/modular/prepare_native_controls.py \
  --output /validation/native-controls \
  --recordings-template /validation/recordings-template.json \
  --maxone-projection /validation/maxone-projection.json \
  --maxtwo-projection /validation/maxtwo-projection.json
```

Mount the same absolute output path inside and outside Docker when the generated
file references will be passed directly to host-launched Nextflow. The generator
records canonical absolute file URIs and never guesses host path translations.
Run the full converter on these inputs before dispatch/preprocessing/sorting.
Then produce matching scientific graphs from the actual converted acquisitions;
existing analyzers from different raw signals, clocks or calibration are invalid
substitutes. Keep real zero/nonempty sorter outcomes and actual NWB producer
acceptance as separate checks.
