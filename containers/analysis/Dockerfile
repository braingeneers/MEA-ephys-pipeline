# Keep the validated scientific stack; add only the two previously task-installed
# report writers, at exactly their original versions and without dependency upgrades.
FROM ghcr.io/allenneuraldynamics/aind-ephys-pipeline-base@sha256:a9728fa63078b0429e4c85b5918d7286d4c3f6ed5def900f9876540513adec03
USER root
RUN python -m pip install --no-cache-dir --no-deps openpyxl==3.1.5 et-xmlfile==2.0.0
USER pipelineuser
