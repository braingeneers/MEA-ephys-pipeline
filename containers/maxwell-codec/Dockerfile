# Keep each original scientific image and add only the verified native codec.
ARG PARENT_IMAGE
FROM ${PARENT_IMAGE}
USER root
COPY --from=braingeneers/modular-maxwell-to-nwb@sha256:c3c204b9338470f5dba010f2214dc96c71e97841bcc36ebac4f950547f8f9eed /opt/maxwell-hdf5-plugin/libcompression.so /opt/mea-maxwell-hdf5-plugin/libcompression.so
RUN echo '4bc74ea98c08b70406aa976950fcf5f55d572430f51d3882eeb947c557dd14f4  /opt/mea-maxwell-hdf5-plugin/libcompression.so' | sha256sum -c -
ENV HDF5_PLUGIN_PATH=/opt/mea-maxwell-hdf5-plugin
USER pipelineuser
