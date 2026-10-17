# Report runtime

Build from the repository root:

```bash
docker build -f containers/analysis/Dockerfile -t braingeneers/mea-ephys-analysis:0.1.0 .
```

This derives from the exact validated scientific base and adds `openpyxl==3.1.5`
and `et-xmlfile==2.0.0`, the versions formerly installed by every report task.
It does not upgrade the scientific dependencies. Reports require this image;
ordinary processing and NWB image pins retain their original meaning.

The direct launch option `--report_container_image` can select an independently
built equivalent image. Keep the base and dependency versions identical for parity.
The release checkpoint records the registry digest after publishing. No scientific
task installs packages or downloads report code from a mutable branch.
