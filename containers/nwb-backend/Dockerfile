ARG PARENT_IMAGE=braingeneers/mea-ephys-nwb:0.2.0
FROM ${PARENT_IMAGE}
USER root
COPY patch.lock.json patch_runtime.py /opt/mea-nwb-backend/
RUN python /opt/mea-nwb-backend/patch_runtime.py
USER pipelineuser
