# MEA electrophysiology pipeline

Nextflow DSL2 processes perform preprocessing, Kilosort4 sorting, analysis,
reporting and derived NWB export. The original multi-backend entry point and sorter
branches remain available in `pipeline/main_multi_backend.nf`.

Use [scientific stages](docs/SCIENTIFIC_STAGES.md) to run stages independently or
compose named subworkflows in one Nextflow graph. They use the same scientific
processes as the original entry point. These stages accept NWB/session directories;
Maxwell acquisition conversion has a separate entry with its own runtime profile;
durable S3 transport for these MEA stages remains a separate adapter.

- [Named stages and local Docker examples](docs/SCIENTIFIC_STAGES.md)
- [Maxwell conversion, bindings and completed local units](docs/MAXWELL_CONVERSION.md)
- [Process settings, resources and compatibility](docs/MODULAR_PROCESSES.md)
- [Portable dependency bundles](docs/PORTABLE_BUNDLES.md)
- [NRP Kubernetes execution](README_NRP.md)
- [NERSC execution](README_NERSC.md)
- [Report image build and release](containers/analysis/README.md)
- [Shared runtime images, overrides and publication state](containers/README.md)
- [Modeled NWB reader dependency layer](containers/nwb-io/README.md)
- [Owning-manager NWB backend](containers/nwb-backend/README.md)
- [Reproducible native modeled validation controls](docs/NATIVE_CONTROLS.md)

Scientific defaults, parameter precedence and original capsule pins are maintained
beside the workflow in `pipeline/`. Build the documented runtime images before
local development; their release records will freeze registry digests.
