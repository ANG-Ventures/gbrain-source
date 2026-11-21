# syntax=docker/dockerfile:1.7
# deploy/stack/cron/Dockerfile: the gbrain cron image (card t_ab2fcba3). The mem0-cron shape
# (mem0-selfhost-deploy stack/cron): FROM the server release image (same bun, same baked app, so `gbrain import`
# in a job runs the exact code that serves), plus only the job toolchain:
#   python3                  gbrainjob dispatcher, audit_lock.py, ingest-chunked's readback lists
#   postgresql-client-16     PGDG, pinned to the server MAJOR: a newer pg_dump emits SQL the PG16 server rejects
#                            (`SET transaction_timeout`, the P4 restore test); the build asserts the major
#   restic 0.19.1            the Studio host's version (`restic version`, 2026-11-20), sha256 verified
#   supercronic v0.2.49      the mem0-cron pin, sha256 verified
#   openssh-client           restic sftp to the NAS
#   procps                   ingest-chunked.sh lists running imports with `ps -axo`
# Build (bin/gbrainctl build does this, one arch per build):
#   docker buildx build --platform linux/arm64 --build-arg SERVER_IMAGE=gbrain-server:<tag> \
#     -f deploy/stack/cron/Dockerfile -t gbrain-cron:<tag> --load deploy/stack/cron
ARG SERVER_IMAGE
FROM ${SERVER_IMAGE}
ARG TARGETARCH
ARG PG_MAJOR=16
ARG RESTIC_VERSION=0.19.1
ARG RESTIC_SHA256_ARM64=a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465
ARG RESTIC_SHA256_AMD64=f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c
ARG SUPERCRONIC_VERSION=v0.2.49
ARG SUPERCRONIC_SHA256_ARM64=02aa0cb229ba09050cba6638059dadb9eedc2276632ea43d6a57a2f8c1629dd5
ARG SUPERCRONIC_SHA256_AMD64=a53ae236602c7338aba3fbaff40bda6300eae3b9fedb8261eb06cfe3724430c1

RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl gnupg python3 openssh-client procps bzip2 tzdata; \
    install -d /usr/share/postgresql-common/pgdg; \
    curl -fsSLo /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc; \
    . /etc/os-release; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends "postgresql-client-${PG_MAJOR}"; \
    rm -rf /var/lib/apt/lists/*; \
    test "$(pg_dump --version | awk '{print $3}' | cut -d. -f1)" = "${PG_MAJOR}"

RUN set -eu; \
    case "${TARGETARCH}" in \
      arm64) rsha="${RESTIC_SHA256_ARM64}"; ssha="${SUPERCRONIC_SHA256_ARM64}" ;; \
      amd64) rsha="${RESTIC_SHA256_AMD64}"; ssha="${SUPERCRONIC_SHA256_AMD64}" ;; \
      *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    cd /tmp; \
    curl -fsSLo restic.bz2 "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_${TARGETARCH}.bz2"; \
    echo "${rsha}  restic.bz2" | sha256sum -c -; \
    bunzip2 restic.bz2; install -m 0755 restic /usr/local/bin/restic; rm -f restic; \
    restic version | grep -q "restic ${RESTIC_VERSION} "; \
    curl -fsSLo /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}"; \
    echo "${ssha}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/supercronic

COPY jobs/ /opt/gbrain-cron/jobs/
COPY lib/ /opt/gbrain-cron/lib/
COPY gbrainjob /usr/local/bin/gbrainjob
COPY crontab /etc/gbrain/crontab
COPY entrypoint.sh /usr/local/bin/gbrain-cron-entrypoint
COPY gbrain /usr/local/bin/gbrain
RUN chmod 0755 /usr/local/bin/gbrainjob /usr/local/bin/gbrain-cron-entrypoint /usr/local/bin/gbrain \
      /opt/gbrain-cron/jobs/*.sh /opt/gbrain-cron/lib/ingest-chunked.sh \
 && for f in /opt/gbrain-cron/jobs/*.sh /opt/gbrain-cron/lib/*.sh; do bash -n "$f" || exit 1; done \
 && python3 -m py_compile /usr/local/bin/gbrainjob /opt/gbrain-cron/lib/audit_lock.py \
 && supercronic -test /etc/gbrain/crontab \
 && git config --system safe.directory '*'

ENV GBRAIN_STATE_DIR=/state GBRAIN_SECRETS_DIR=/state/secrets GBRAIN_JOBS_DIR=/opt/gbrain-cron/jobs PGPASSFILE=/state/.pgpass HOME=/state
WORKDIR /state
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/gbrain-cron-entrypoint"]
CMD []
