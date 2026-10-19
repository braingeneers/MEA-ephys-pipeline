# Portable local scientific bundles

`pipeline/helpers/portable_bundle.py` copies declared scientific payloads, rebases
their executable SpikeInterface references, and verifies their scientific identity.
It runs in the pinned pipeline base image; it does not change preprocessing,
sorting, curation or reporting algorithms.

An analyzer that loads without error can still have `recording=None`: the pinned
SpikeInterface loader catches missing-recording errors. The bundle verifier checks
the serialized recording first, requires the recording to load, and reads samples.
It preserves the analyzer's original lazy recording graph. A binary cache is not a
substitute for a lazy filtered recording, whose floating samples can differ.

## Layout and identity

```text
<unit-id>.<stage>/
  bundle.json
  data/
    ecephys_session/                 # input NWB(s) and supplied session metadata
    job_<index>.json                 # or the capsule's .pkl fallback
    preprocessed_<recording>/        # real binary, channel/probe/timing metadata
    binary_<recording>.json
    preprocessed_<recording>.json
    preprocessedviz_<recording>.json
    motion_<recording>/
    spikesorted_<recording>/
    postprocessed_<recording>.zarr/
    ...                             # the stage's declared remaining artifacts
```

The contract is `mea.local-stage-bundle/v1`. `unit_id` namespaces whole bundles;
internal recording names and filenames stay unchanged. Two units may each contain
`synthetic.nwb`; they must have distinct external unit identities. A non-conversion
bundle's `recording_ids` must exactly match its dispatch inventory. Never join units
by arrival order or a raw basename.

`bundle.json` records the stage (`nwb`, `prepared`, `sorted`, `analyzed`, `reported`
or `exported`), recording identities, small caller metadata, all payload sizes and
SHA-256 values, executable serialization descriptors and scientific fingerprints.
Its content digest detects manifest drift. It is not an authentication signature,
publication receipt, S3 stage manifest or promise of cross-run result reuse.

Copying an analyzer alone is insufficient. Include the complete raw NWB/session
and the binary/motion dependencies required by that unit. Bundles contain real
files, without symlinks back to producing tasks. The original synthetic fixture's
analyzed bundle is approximately 731 MB: 350 MB raw NWB, 346 MB prepared binary and
the remaining analyzer/visualization/metadata files. Keep that decoded/cumulative
volume in resource planning. Future S3 materialization should reference unchanged
verified predecessor objects instead of uploading each cumulative copy again.

## Assembly API

Run the helper in the same scientific runtime that produced the serializations:

```bash
python pipeline/helpers/portable_bundle.py pack --spec assembly.json --output unit.prepared
python pipeline/helpers/portable_bundle.py verify --bundle unit.prepared
```

Assembly creates a new directory and never overwrites an existing bundle. Failure
leaves an incomplete directory without a completion manifest. The caller can retain
it for diagnosis or remove it through its ordinary task-cleanup policy.

The specification has these fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | `1` |
| `unit_id` | Stable path-safe outer identity |
| `stage` | Required bundle stage kind |
| `recording_ids` | Unique internal recording identities |
| `payloads` | List of `{source, destination}` POSIX paths; destinations are relative and under `data/` |
| `serializations` | Complete executable dependency catalogue described below |
| `dependency_aliases` | Optional explicit `{source, destination}` bindings for equivalent staged producer copies; every file is checked before binding |
| `metadata` | Optional small caller-owned provenance/settings object |

Each serialization descriptor has `format`, `source`, `path`, `base`, optional
`pointer`, and `relative_to`. `source` and `base` are explicit assembly-time POSIX
paths. `path` is the copied file or analyzer directory under `data/`; `relative_to`
is its portable loading base. `pointer` is a JSON-style list of dictionary keys and
array indexes, empty for a top-level extractor. Formats are `extractor_json`,
`extractor_pickle` and `analyzer_zarr`. The original capsule pickle fallback retains
its arrays and typed properties; it is read as a workflow-produced serialization.

`capsule_serializations(source, destination, data_base)` describes the known capsule
outputs. Supply the invocation's actual output and input bases explicitly. Dispatch
recording dictionaries and visualization timeseries use the capsule input base;
binary/lazy preprocessing dictionaries and ordinary drift use the output base.
Skipped-preprocessing drift uses the input base. Binary-folder dictionaries use
their own directory; an analyzer's Zarr `recording` dataset uses its analyzer
directory. Do not derive those bases from a raw filename or assume a predecessor
workspace will remain mounted.

The collector rewrites analyzer graphs relative to its future publication directory,
so its analyzer descriptor must supply that explicit `base`. Source fingerprinting
loads the recording with the declared base and passes it to the analyzer loader.
The portable result uses its own analyzer directory after rebasing. Copy aliases
compare full SHA-256 identities recursively; JSON may differ only in whitespace,
with unchanged parsed types/values and dictionary order. The visualization capsule
uses timeseries dictionary order for plot rows, so executable serializations retain
that order. Canonical key sorting is reserved for manifest hashes. Aliases never
bind by basename alone.

The helper changes executable filesystem keys in extractor kwargs and their
metadata-folder references. NWB-internal `electrical_series_path` remains unchanged.
Every dependency must map to declared copied content. It rejects ambiguous source
mappings, destination collisions, escaping paths, unsupported executable formats,
malformed graphs and omissions from the known executable catalogue. Original
`provenance.json`, sorter history, annotations and processing paths remain as
historical evidence; they are not replayed as executable dependencies.

## Verification scope

Before accepting a bundle, assembly compares the original and portable graphs:
channel order, properties (including gains/offsets/geometry), sample counts/rates,
start/middle/end raw and microvolt sample windows and acquisition times, unit IDs,
all spike-vector values, selected extension API data and every saved extension file.
All copied files are verified against their full source SHA-256 values before
serialization changes. Saved scientific arrays/extensions are not recomputed.
Fingerprints preserve nullable Pandas values, NaN and infinity as distinct values;
they do not remove or repair undefined scientific measurements.

Verification repeats the payload inventory and scientific checks and rejects
missing, changed or unexpected files. Call it before a downstream consumer uses a
materialized bundle. The catalogue digest alone does not prove a recording remains
loadable. A local JSON manifest also does not replace converter validation or
complete S3 identity/publication checks.

Named scientific stages also validate the incoming unit metadata before releasing
the verified input to downstream processes. Every predecessor key must remain
present with its exact encoded value; callers may add new keys. This preserves
projection/session/recording references, nullable values, integer/float/boolean
types, large integers and signed zero. The same condition applies during assembly.

For an explicit caller claim, use the scientific stage helper in the pinned base
runtime, supplying the owning sorter outcome classifier:

```bash
python pipeline/helpers/stage_bundle.py --outcomes-code capsules/kilosort4/code/outcomes.py \
    verify --bundle unit.reported --kind reported --unit-id unit --unit-meta incoming-unit-meta.json
```

Omitting `--unit-meta` retains the independent contents-only verification API. All
maintained named stage callers supply it; that check must happen before downstream
science or publication, rather than waiting for the final packer to reject a
changed reference. The ordinary entry point merges omitted metadata from its input
bundle; named callers should forward the predecessor's metadata explicitly.

The regression helper is `scripts/modular/check_portable_bundle.py`. Its `build`
command takes a retained real direct run, source fixture/session and a fresh output
directory. Move the resulting `unit-a`/`unit-b` plus `assembly-spec.json` to a new
directory, then run its `verify --bundle <directory>` command in a container mounting
only that directory and helper/test source. It asserts original producer paths are
unavailable. This also covers an empty analyzer, colliding raw basenames, the pickle
fallback, wrong recording identity, malformed/missing/escaping dependencies and
tampered payloads. It does not establish independent Nextflow stage execution,
GPU equivalence or NERSC scheduling.
