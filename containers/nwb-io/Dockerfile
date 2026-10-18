# NWB I/O candidate; retain the original numerical and SpikeInterface packages.
FROM ghcr.io/allenneuraldynamics/aind-ephys-pipeline-nwb@sha256:03ad8e8572ad1e98da9d35c380e8fad23de895731d2d382fd9e18376dd61ab4b
USER root
COPY --from=braingeneers/modular-maxwell-to-nwb@sha256:c3c204b9338470f5dba010f2214dc96c71e97841bcc36ebac4f950547f8f9eed /opt/maxwell-hdf5-plugin/libcompression.so /opt/mea-maxwell-hdf5-plugin/libcompression.so
RUN echo '4bc74ea98c08b70406aa976950fcf5f55d572430f51d3882eeb947c557dd14f4  /opt/mea-maxwell-hdf5-plugin/libcompression.so' | sha256sum -c -
ENV HDF5_PLUGIN_PATH=/opt/mea-maxwell-hdf5-plugin
COPY requirements.lock artifacts.lock.json /opt/mea-nwb-io/
RUN --mount=type=bind,source=wheelhouse,target=/tmp/wheelhouse python -m pip install --no-cache-dir --no-index --find-links /tmp/wheelhouse --no-deps --require-hashes -r /opt/mea-nwb-io/requirements.lock
RUN python -m pip check
RUN python -c "import pynwb, ndx_cell_culture, hdmf_zarr; from neuroconv.tools.nwb_helpers import configure_backend, get_default_backend_configuration; from neuroconv.tools.spikeinterface.spikeinterface import add_recording_to_nwbfile, add_electrodes_info_to_nwbfile; print('NWB_IO_IMPORTS_OK')"
USER pipelineuser
