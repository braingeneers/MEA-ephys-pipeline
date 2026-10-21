# Report runtime

The published0.1.0 image is the build parent for the final0.2.0 report runtime.
Both immutable references are in [the runtime index](../runtime-images.json).
For a new local development parent, build from the repository root:

```bash
docker build -f containers/analysis/Dockerfile -t braingeneers/mea-ephys-analysis:development-parent .
```

This derives from the exact validated scientific base and adds `openpyxl==3.1.5`
and `et-xmlfile==2.0.0`, the versions formerly installed by every report task.
It does not upgrade the scientific dependencies. Reports require this image;
ordinary processing and NWB image pins retain their original meaning.

The direct launch option `--report_container_image` can select an independently
built equivalent image. Keep the base and dependency versions identical for parity.
The release record contains its verified registry digest. No scientific
task installs packages or downloads report code from a mutable branch.
