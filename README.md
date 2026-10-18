# MEA electrophysiology pipeline

Nextflow DSL2 processes perform preprocessing, Kilosort4 sorting, analysis,
reporting and derived NWB export. The original multi-backend entry point and sorter
branches remain available in `pipeline/main_multi_backend.nf`.

Use [scientific stages](docs/SCIENTIFIC_STAGES.md) to run stages independently or
compose named subworkflows in one Nextflow graph. They use the same scientific
processes as the original entry point. These stages accept NWB/session directories;
vendor acquisition conversion and durable S3 transfer require separate adapters.

- [Named stages and local Docker examples](docs/SCIENTIFIC_STAGES.md)
- [Process settings, resources and compatibility](docs/MODULAR_PROCESSES.md)
- [Portable dependency bundles](docs/PORTABLE_BUNDLES.md)
- [NRP Kubernetes execution](README_NRP.md)
- [NERSC execution](README_NERSC.md)
- [Report image build and release](containers/analysis/README.md)

Scientific defaults, parameter precedence and original capsule pins are maintained
beside the workflow in `pipeline/`. Build the derived report image as documented
before local development; its release record will freeze the registry digest.
