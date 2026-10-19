# Independent NWB stored-data validation

`scripts/modular/nwb_data_guard.py` supplements schema, modeled metadata and
logical scientific comparisons for the pinned Ecephys/Units Zarr writers. Logical
unit trains alone do not cover their complete stored target buffers: an unused
spike tail can remain schema-valid. Keep these scopes separate when reporting
acceptance.

Run the QA tools in the exact reviewed Python 3.11 writer/reader containers; see
[runtime ownership](../containers/README.md) and the
[NWB backend](../containers/nwb-backend/README.md). No installation or production
runtime change is needed. Use fresh evidence directories and read-only inputs.

Compile expectations with only original input HDF5 and independent input-derived
facts/arrays mounted. Produced Ecephys/Units stores must be unavailable:

```bash
python scripts/modular/check_nwb_data.py compile \
  --source /inputs/output.nwb --facts /facts/unit/facts.json \
  --arrays /facts --output /reports/expected-storage.json
```

The fact contract contains `source.header` (complete modeled source graph, source
UUIDs and Data payloads), the exact source HDF5 SHA-256, source electrode mapping
and device name; `neural.raw`/`neural.lfp` sample/clock descriptors and region rows;
and `units` properties, waveforms, full per-unit spike-time descriptors, electrode
rows, table IDs and expected column membership. Numeric descriptors have `kind`,
relative `file`, exact `dtype`/`shape` and full `.npy` SHA-256. Text descriptors retain
input dtype, shape and every value. Original metric missing encodings must be kept
separately from explicit export transforms. Never construct these facts from a
produced writer output or change them to obtain equality.

Compilation reads the original metadata datasets and derives all added signal,
clock, region, property, waveform, spike-target and cumulative-index expectations.
It retains each input's own source/model identity. It explicitly models HDMF's
uint8 → uint16 → uint32 → uint64 index promotion, signed int64 electrode rows,
the pinned backend text/reference codecs and scalar-text representation. Generated
UUID4 names/objects are validated observations; source identities remain exact.

Then verify a physically copied output with original inputs/work roots unavailable:

```bash
python scripts/modular/check_nwb_data.py verify \
  --nwb /outputs/units.nwb --expected /expected/expected-storage.json \
  --role nwb_units --report /reports/stored-data.json
```

Use `--role nwb_ecephys` for the intermediate store and add `--write-raw` only for
an explicitly raw-enabled producer. The guard requires exact dataset membership
outside the declared `specifications` schema cache, complete consolidated/physical
metadata agreement, every stored dtype/shape/value and canonical reference binding.
It binds each decoded HDMF Data object to its physical array and rejects unknown
datasets, unused tails, incorrect boundaries/types and foreign bindings. Numeric
comparison preserves signed zero and all bits except canonical NaN payloads. Text
types and Boolean/integer/float distinctions remain explicit. The CLI also runs
schema validation; complete metadata and logical scientific checks are still needed.
The numeric controls retain each finite complex component independently when the
other component is NaN, and separately verify signed zero, NaN positions and the
declared equivalent-NaN-payload rule.

`check_nwb_data_controls.py --nwb ... --raw-nwb ... --expected ... --output ...`
creates fresh positive copies and independent scientific corruptions for both raw
settings. Use the positive reference fixtures, including `default_qc` and the
`electrode` source column. It retains replaced bytes outside each changed store,
verifies originals are unchanged and requires the intended guard rejection.
Float32 spike times are a separately recorded schema-invalid control because the
pinned Units schema requires float64; they cannot prove scientific-guard coverage.
Run both Maxwell layouts, actual positive/zero outcomes and both writer roles for
the full current matrix. No small guard test alone establishes pipeline acceptance.
