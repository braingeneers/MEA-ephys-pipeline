ARG PARENT_IMAGE=braingeneers/mea-ephys-base:0.1.0
FROM ${PARENT_IMAGE}
ARG MEA_SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/braingeneers/MEA-ephys-pipeline" \
      org.opencontainers.image.revision="${MEA_SOURCE_REVISION}" \
      org.opencontainers.image.description="MEA export verification: base science with compatible NWB readers"
USER root
COPY artifacts.lock.json requirements.lock install.py /opt/mea-export-verifier/
COPY wheelhouse/ /opt/mea-export-verifier/wheelhouse/
RUN python /opt/mea-export-verifier/install.py
USER pipelineuser
