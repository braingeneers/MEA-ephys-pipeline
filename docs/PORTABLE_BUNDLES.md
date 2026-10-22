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

The contract is `mea.local-stage-bundle/v2`, with exact integer `schema_version: 2`.
The assembly specification and stage request remain separate exact-integer v1
interfaces. `unit_id` namespaces whole bundles;
internal recording names and filenames stay unchanged. Two units may each contain
`synthetic.nwb`; they must have distinct external unit identities. A non-conversion
bundle's `recording_ids` must exactly match its dispatch inventory. Never join units
by arrival order or a raw basename.

`bundle.json` records the stage (`nwb`, `prepared`, `sorted`, `analyzed`, `reported`
or `exported`), recording identities, small caller metadata, all payload sizes and
SHA-256 values, executable serialization descriptors, complete scientific
fingerprints and separate producer-owned `executable_bindings`.
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

Portable executable paths must themselves be relative, even when an absolute
spelling currently resolves to the same bundled file. Stage verification preflights
these graph paths after incoming metadata and before native/work policy constructs
extractors. Assembly still accepts the original capsules' absolute source paths and
rebases them. Existing valid v2 receipts keep their format and scientific identities.

## Producer origins

Before stage-out, the producer wrapper normalizes owned executable dependency
metadata using its live scratch directory and declared inputs. This keeps raw
intermediate artifacts loadable by ordinary scientific capsules. It preserves
dictionary order, non-path metadata, numerical arrays and extension data; native
binary-folder sidecars keep their required co-located relative representation.
Missing or undeclared dependencies fail before the output port is emitted.
The helper sources and collector's explicit CLI bases participate in task identity.
These intermediate absolute paths are subsequently rebased into the portable
bundle's strictly relative executable paths by the existing full validator.

Scratch execution can leave a valid lazy graph relative to a directory that no
longer exists after stage-out. New producer wrappers record that original
coordinate explicitly before returning. The reserved
`.mea-producer-location-<binding>.json` file is historical provenance, copied with
the scientific payload. It records the actual producer capsule root, shared
publication root, separate emitted results port, role, recorder code identity
and observed symlink bindings. No origin is inferred from filenames or task hashes.

An assembly-time serialization descriptor may include `producer_location` with
the decoded `record`, its declared `source` file and complete `sha256`. The packer
requires the source and copied inventory bytes to match that record before using
it. Recorded links resolve before `..` traversal; paths owned by the retired
producer then map to its declared shared outputs. Every resulting dependency
still needs an ordinary declared payload or byte-checked alias. An origin cannot
authorize an undeclared file or replace a recording with a binary cache.

Active portable descriptors omit these source instructions. The corresponding
`original_serializations` entry retains the record and copied provenance binding;
all active graph paths remain relative to the bundle. Existing valid artifacts
without producer records keep their explicit-base behavior. New wrappers require
their own matching record, rejecting missing, ambiguous or malformed origins.

## Verification scope

Before accepting a bundle, assembly compares the original and portable graphs:
channel order, properties (including gains/offsets/geometry), sample counts/rates,
every raw and calibrated microvolt sample and every acquisition time, unit IDs,
all spike-vector values, selected extension API data and every saved extension file.
All copied files are verified against their full source SHA-256 values before
serialization changes. Saved scientific arrays/extensions are not recomputed.
Fingerprints preserve nullable Pandas values, NaN and infinity as distinct values;
they do not remove or repair undefined scientific measurements.

Recording arrays use exact shape/dtype and streaming SHA-256 of all ordered values.
The fixed `mea.recording-stream/v1` policy reads 65,536 frames per block, including
the final partial block; it never samples or shortens recordings. Explicit clocks
are sliced from their actual stored vectors. Regular clocks use SpikeInterface's
exact float64 global-index/rate/start equation. Read boundaries are fixed so lazy
extractors receive the same calls before and after relocation. No numeric tolerance
or scientific algorithm change is introduced by packaging.

After all serialization rewrites, assembly freezes each active graph's class,
module, version, kwargs, path mode and folder metadata. These bindings preserve
selector types and dictionary order, normalize paths to logical locations under
`data/`, and bind the complete bytes of every active file/directory dependency,
including NWB headers. Verification checks them separately from the ordinary file
inventory. Historical annotations remain provenance. A receiver resealing transport
checksums cannot make a changed recording, header or executable source match the
original producer receipt. These local receipts are not signatures and do not
replace independent scientific validation.

Current consumers reject v1 bundles with an actionable legacy-format error: those
receipts recorded sample windows and cannot attest to complete recordings. Re-run
the named stage producer to obtain v2. Audit historical artifacts with their exact
frozen original verifier; there is no implicit production upgrade or bypass. Any
test-only v2 counterpart must cite preexisting independently verified original
science, retain its original producer attribution and record new packaging separately.

Verification repeats the payload inventory and scientific checks and rejects
missing, changed or unexpected files. Call it before a downstream consumer uses a
materialized bundle. The catalogue digest alone does not prove a recording remains
loadable. A local JSON manifest also does not replace converter validation or
complete S3 identity/publication checks.

Named scientific stages check manifest/inventory and kind/unit, incoming metadata,
native conversion/work/outcome policy, then complete scientific/binding receipts
before releasing
the verified input to downstream processes. Every predecessor key must remain
present with its exact encoded value; callers may add new keys. This preserves
projection/session/recording references, nullable values, integer/float/boolean
types, large integers and signed zero. The same condition applies during assembly.

Prepared and later bundles require the capsule's per-recording
`data_process_preprocessing_<recording>.json`; sorted and later bundles also require
`data_process_spikesorting_<recording>.json`. The shared producer/consumer policy
checks their JSON structure, process owner, recording or sorter identity, and ordered
completion timestamps. This lightweight required-work check supplements the actual
recording, sorting and log checks; it does not replace scientific or AIND validation.
Unknown sorter errors and incomplete work remain hard failures. A successful empty
sort requires a completed successful sorter log; `no_spikes_detected` requires the
exact owned zero-detection error, completed detection evidence and explicit status.
Its frame counts and sampling rate must retain their exact encoded numeric types.

Reported and exported bundles additionally require `metadata.reported_science`,
with exact-integer `schema_version: 1` and ordered per-recording identities. Reported
assembly creates it after checking every reported train against its actual analyzer
unit and sample clock. Report selection remains independent of the external QC mask.
The receipt contains the ordered typed unit keys, complete train dtype/shape/value
identities, and an identity for each decoded summary and burst field. The
`mea.typed-json/v1` field encoding preserves object membership, array order, scalar
types and binary64 signs; it includes events, diagnostics and plotting values.
These are scientific value identities, separate from encoded-file inventory hashes.
Exported assembly retains the predecessor's report receipt without recapturing it.

The shared verifier requires the receipt and rejects exact differing scientific
fields. It does not create missing expectations. Historical v2 bundles predating
this requirement retain their original reader and scientific attribution; explicit
QA metadata-only authoring must bind immutable original science independently.
Run `scripts/modular/check_report_science.py --output <new-directory>` in the pinned
base runtime for focused contract regressions, optionally supplying both
`--positive-bundle` and `--zero-bundle` for readonly real report inputs. These checks
run no scientific capsules and do not substitute for full bundle or Nextflow acceptance.

Exported bundles require `metadata.derived_science`, with exact-integer schema1.
Assembly freezes the complete output set, formats and every stored group/dataset,
physical attribute, typed Data buffer and ordered reference binding. Numeric arrays
use bounded C-order reads; object arrays retain complete typed values and reference
targets. Shapes, dtypes, signed zero and nonfinite masks remain exact. This covers
complete ragged buffers/indices, identifiers, metrics, waveforms, raw/LFP samples
and clocks, model metadata and aliases. The receipt records measured generated IDs;
it supplements independent input-derived writer validation rather than replacing it.

HDF5 exports retain their internal hard/soft alias graph and must be self-contained.
External links, external/virtual data and unsupported region references fail
explicitly. Zarr references and `zarr_link` targets must resolve within the store,
and consolidated metadata must match the physical metadata. Directory-backed
stores may have a `.nwb` suffix. Verification requires the existing receipt and
identifies the differing scientific node and field. It never manufactures missing
expectations or upgrades a historical artifact.

For an explicit caller claim on a reported bundle, use the scientific stage helper
in the pinned base runtime, supplying the owning sorter outcome classifier:

```bash
python pipeline/helpers/stage_bundle.py --outcomes-code capsules/kilosort4/code/outcomes.py \
    verify --bundle unit.reported --kind reported --unit-id unit --unit-meta incoming-unit-meta.json
```

Run the same helper with `--kind exported` in the configured
`container_images.export_verifier` image. That workflow-owned runtime preserves
the base numerical stack and exact scientific receipts while enabling concrete
NWB reference decoding; the scientific writer image has a separate stack. Local
integration currently uses the versioned candidate documented in
[its recipe](../containers/export-verifier/README.md). Full stage release and
registry publication remain pending. Focused contract checks run through
`scripts/modular/check_export_science.py --output <new-directory>` in that image;
its tiny schema-valid fixtures are separate from full scientific acceptance.

Omitting `--unit-meta` retains the independent contents-only verification API. All
maintained named stage callers supply it; that check must happen before downstream
science or publication, rather than waiting for the final packer to reject a
changed reference. The ordinary entry point merges omitted metadata from its input
bundle; named callers should forward the predecessor's metadata explicitly.

A reserved `data/ecephys_session/conversion-unit.json`, stored native metadata or an
incoming native declaration requires native identity validation. Removing the
stored marker cannot turn a native artifact into a generic input. Generic inputs
without native declarations retain the same interface. Full native modeled/schema/
companion validation remains owned by the converter runtime.

Use `scripts/modular/check_complete_receipts.py create-nwb --output <fixture.nwb>`
in the pinned NWB image once, then `verify --source-nwb <fixture.nwb> --output
<new-directory>` in the pinned base image for generated multi-segment sample/clock/scaling, schema/type,
graph/header/dependency, native-declaration and metadata-order controls. These
technical fixtures supplement full-size science and actual Nextflow acceptance.

`scripts/modular/check_stage_work.py` accepts `--preprocessing-record`,
`--sorting-record`, their `--recording-id`, a genuine `--zero-sorter-log`, and a new
`--output` directory. Supply real retained capsule records in the pinned base runtime.
It preserves them and writes missing/malformed metadata and terminal-log controls;
it generates no sorting result.

For explicitly reviewed historical test data, `scripts/modular/import_verified_bundle.py`
creates a separate v2 counterpart. It requires a preexisting corpus authority and its
SHA-256, a label, explicit authority path mappings, and the frozen legacy source plus
its complete source inventory and SHA-256. Mount original data and authorities read
only. Supply a new `--output` directory and an external `--report` path. The tool checks
all original files and scientific evidence references, runs the frozen legacy CLI,
freezes complete original expectations, then packages and verifies the counterpart.
It requires identical payload bytes and preserves historical scientific metadata and
serialization provenance; the external report identifies the new packaging separately.
Failed attempts remain for diagnosis. This test tool is not a production upgrade path.

The regression helper is `scripts/modular/check_portable_bundle.py`. Its `build`
command takes a retained real direct run, source fixture/session and a fresh output
directory. Move the resulting `unit-a`/`unit-b` plus `assembly-spec.json` to a new
directory, then run its `verify --bundle <directory>` command in a container mounting
only that directory and helper/test source. It asserts original producer paths are
unavailable. This also covers an empty analyzer, colliding raw basenames, the pickle
fallback, wrong recording identity, malformed/missing/escaping dependencies and
tampered payloads. It does not establish independent Nextflow stage execution,
GPU equivalence or NERSC scheduling.
