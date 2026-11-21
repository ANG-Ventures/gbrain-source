# syntax=docker/dockerfile:1.7
# deploy/stack/server/Dockerfile: the gbrain server image (card t_ab2fcba3).
#
# Built FROM A COMMIT of this repo, never a live checkout: the build context is the repo root at the commit
# gbrainctl checks out into a clean worktree, and the commit sha is baked in as a label + GBRAIN_REVISION, so
# `docker inspect` answers "what code is serving" without a host tree (the old deploy-parity check read
# ~/gbrain/app). One arch per build: linux/arm64 for the Studio, linux/amd64 for the NAS host.
#   docker buildx build --platform linux/arm64 --build-arg GBRAIN_REVISION=<sha> \
#     -f deploy/stack/server/Dockerfile -t gbrain-server:<sha12>-arm64 --load .
# (bin/gbrainctl build does exactly this.)
#
# bun is pinned to 1.3.10, the version the launchd serve ran (`bun --version` on the Studio, 2026-11-20), by
# multi-arch index digest. Dependencies come from bun.lock with --frozen-lockfile. --ignore-scripts skips
# scripts/postinstall.ts, which would try `gbrain apply-migrations` against whatever DB it could find at build
# time; migrations are an operator step, not an image build side effect.
ARG BUN_IMAGE=oven/bun:1.3.10-debian@sha256:367842b35abbdf23f39e23c71f3a08eee940ff2679a14e08a5afcf4a1436cd89
FROM ${BUN_IMAGE}
ARG GBRAIN_REVISION=unknown
# The non-root user the server (and the cron image built FROM this one) runs as. gbrainctl build passes the host
# file's stack_uid/stack_gid: the owner of the rendered 0600 secrets and of the brain repo, so a bind-mounted secret
# is readable and a brain commit keeps the host's ownership on a strict (Linux) runtime too. 10001 = a bare build.
ARG GBRAIN_UID=10001
ARG GBRAIN_GID=10001

# git: the import walker lists files with `git ls-files` (src/commands/import.ts); tini: PID 1 reaps the
# children `serve` and the cron jobs spawn; ca-certificates: OpenAI / DeepInfra TLS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# The ONE passwd entry for the uid (ssh refuses to run without one, and reads ~/.ssh from it): a base-image user
# already on that uid (bun is 1000) is removed first, so getpwuid() returns `gbrain`, not a stale home. The gid may
# already exist (dialout is 20; the name does not matter). /gbrain (GBRAIN_HOME, the gbrain-home volume) is created
# owned by the uid, so a fresh named volume inherits that owner.
RUN if u="$(getent passwd "${GBRAIN_UID}" | cut -d: -f1)" && [ -n "$u" ]; then userdel "$u"; fi \
 && (getent group "${GBRAIN_GID}" >/dev/null || groupadd -g "${GBRAIN_GID}" gbrain) \
 && useradd -u "${GBRAIN_UID}" -g "${GBRAIN_GID}" -d /gbrain -M -s /usr/sbin/nologin gbrain \
 && install -d -o "${GBRAIN_UID}" -g "${GBRAIN_GID}" -m 0700 /gbrain

WORKDIR /app
COPY package.json bun.lock bunfig.toml ./
COPY scripts/postinstall.ts scripts/postinstall.ts
RUN bun install --frozen-lockfile --ignore-scripts
COPY . .
COPY deploy/stack/server/entrypoint.sh /usr/local/bin/gbrain-entrypoint
RUN chmod 0755 /usr/local/bin/gbrain-entrypoint \
 && bun run src/cli.ts --version >/dev/null \
 && printf '%s\n' "${GBRAIN_REVISION}" > /app/REVISION

LABEL org.opencontainers.image.source="https://github.com/ANG-Ventures/gbrain-source" \
      org.opencontainers.image.revision="${GBRAIN_REVISION}"
ENV GBRAIN_REVISION=${GBRAIN_REVISION} \
    GBRAIN_HOME=/gbrain \
    GBRAIN_LINK_RESOLUTION_GLOBAL_BASENAME=1 \
    NODE_ENV=production
USER ${GBRAIN_UID}:${GBRAIN_GID}
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/gbrain-entrypoint"]
CMD ["serve", "--http", "--port", "8199", "--bind", "0.0.0.0"]
