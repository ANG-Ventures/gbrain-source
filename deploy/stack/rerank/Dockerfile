# syntax=docker/dockerfile:1.7
# deploy/stack/rerank/Dockerfile: the gbrain rerank path (card t_ab2fcba3). ONE image, two services:
#   rerank-proxy   python3 proxy.py: zerank-2 primary, DeepInfra Qwen3 fallback (stdlib only)
#   rerank-tunnel  tunnel.sh: ssh -L to zerank-native on the reranker host, with the launchd wrapper's backoff
# python:3.12-slim (bookworm) by multi-arch index digest; openssh-client from Debian.
ARG PY_IMAGE=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
FROM ${PY_IMAGE}
ARG GBRAIN_REVISION=unknown
# Non-root, the same uid as the other stack images (gbrainctl build passes hosts/<h>.toml stack_uid/stack_gid): the
# proxy reads the 0600 deepinfra-key and the tunnel the 0600 ssh key, both bind-mounted as the rendering user's files.
# A passwd entry with a home is needed by ssh. Nothing here writes outside /tmp.
ARG GBRAIN_UID=10001
ARG GBRAIN_GID=10001
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client tini \
 && rm -rf /var/lib/apt/lists/* \
 && if u="$(getent passwd "${GBRAIN_UID}" | cut -d: -f1)" && [ -n "$u" ]; then userdel "$u"; fi \
 && (getent group "${GBRAIN_GID}" >/dev/null || groupadd -g "${GBRAIN_GID}" gbrain) \
 && useradd -m -u "${GBRAIN_UID}" -g "${GBRAIN_GID}" -s /usr/sbin/nologin gbrain
COPY proxy.py /opt/gbrain-rerank/proxy.py
COPY tunnel.sh /opt/gbrain-rerank/tunnel.sh
RUN chmod 0755 /opt/gbrain-rerank/tunnel.sh && python3 -m py_compile /opt/gbrain-rerank/proxy.py && sh -n /opt/gbrain-rerank/tunnel.sh
LABEL org.opencontainers.image.source="https://github.com/ANG-Ventures/gbrain-source" \
      org.opencontainers.image.revision="${GBRAIN_REVISION}"
ENV GBRAIN_REVISION=${GBRAIN_REVISION} PYTHONUNBUFFERED=1
USER ${GBRAIN_UID}:${GBRAIN_GID}
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/opt/gbrain-rerank/proxy.py"]
