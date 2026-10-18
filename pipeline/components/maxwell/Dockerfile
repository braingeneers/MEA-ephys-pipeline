FROM davidparks21/maxwell-to-nwb@sha256:8486ce5336d922dd81a7f95a40803eb64514a68e37a03d998183284aba11cdaf

ARG SOURCE_REVISION=development
LABEL org.opencontainers.image.source="https://github.com/braingeneers/workflows" \
    org.opencontainers.image.revision="$SOURCE_REVISION" \
    org.opencontainers.image.version="0.1.3"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/ephys-modular/maxwell/scripts
WORKDIR /opt/ephys-modular/maxwell
COPY scripts ./scripts
COPY defaults.json ./defaults.json
COPY process-policy.json ./process-policy.json
COPY extraction.json ./extraction.json
COPY runtime-lock.json ./runtime-lock.json
RUN python scripts/runtime_identity.py

# PyNWB/HDMF caches must also work for an arbitrary non-root task UID. Set this
# after the build probe so its root-owned cache is not baked at the runtime path.
ENV XDG_CACHE_HOME=/tmp/ephys-modular-cache

CMD ["python", "scripts/convert_maxone_to_nwb.py", "--help"]
