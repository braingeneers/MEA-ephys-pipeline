# Generated Maxwell conversion component

Generated from https://github.com/braingeneers/workflows at `beaa98304999034d28867ca218ff08ea0305820e`.
Owning workflow version: `0.1.24`. Scientific task image: `braingeneers/modular-maxwell-to-nwb@sha256:cd573dead65220ca87469e61e1dcf40ebe999fff6d48c9b9f0d504c86feb34ce`.
Generated scientific/controller files retain their upstream bytes and headers.
Only Docker COPY source paths are relocated. No license is inferred or reassigned;
consult the recorded upstream notices/headers and source provenance.

This is the complete fresh conversion component: source validation, conversion,
channel-major repack, compact companion, LINDI, final validation and verified
publication. Source replacement/finalization is not supported. The native artifact
contract is preserved. An MEA artifact-to-local-unit bridge is a separate caller.

Requires Nextflow 26.04.6 and Docker for local tasks. Its config includes the pinned
nf-k8s deployment plugin; driver plugin acquisition is distinct from scientific
tasks, which require no package or Maxwell plugin downloads. Keep this config
separate from older KS4/NERSC entry profiles. The root lib directory supplies
MaxwellTaskRetry; foreign imports must explicitly arrange its classpath.

Run from a separate launch directory, using absolute paths for all local inputs
and outputs. A standalone local `params.json` can contain:

```json
{"recordings":[{"input_uri":"file:///data/recording.raw.h5","data_format":"maxone"}],
 "output_uri":"file:///data/results/","collection_uri":"file:///data/results/derived/nwb/",
 "settings":{}}
```

```bash
nextflow run /path/to/component/main.nf -profile docker -params-file /path/to/params.json
python3.11 /path/to/component/export_component.py verify /path/to/component
```

Local file transport is explicit and standalone-only. S3 callers provide complete
recordings or verified artifact references, explicit output/collection/cache roots
where needed, a public reader endpoint and normal credentials. The workflow owns
transfers/verification and emits only after publication. Do not mix full runtime
configs into other workflows. See main.nf/component.nf for the exact native ports.

For exact JSON numeric fidelity, pass recordings/settings as JSON strings or call
the exported maxwellJson(text) before creating value channels. Its pinned-runtime
Jackson decoder retains arbitrary-precision integers and finite binary64 floats,
including negative zero. It rejects duplicate keys, trailing content and non-finite
values. Nextflow's own params-file object reader and Groovy JsonSlurper can alter
numbers before this component receives them; typed inputs retain their supplied
values, not a recoverable copy of the original bytes.

The complete tree includes image build source as well as staged runtime. Consumers
may select a smaller explicit runtime closure; do not ship tests/scientific data or
discard common schemas/libraries. export-manifest.json records roles and hashes.

Regenerate from the exact committed exporter, with Python 3.11+ and Git:

```bash
python3.11 export_component.py export --source-repository /path/to/workflows \
  --source-revision beaa98304999034d28867ca218ff08ea0305820e --output /path/to/new-component
```

For a local development rebuild (not a published image or replacement release pin):

```bash
docker build --network none --build-arg SOURCE_REVISION=beaa98304999034d28867ca218ff08ea0305820e \
  -t maxwell-export-check:development /path/to/component
docker run --rm --network none --entrypoint python maxwell-export-check:development \
  scripts/runtime_identity.py
```

The pinned base image must already be available. Scientific dependencies, plugin
bytes and extraction/runtime locks remain unchanged. Rebuilt image identity can
differ from the published task image; record that development identity explicitly.
