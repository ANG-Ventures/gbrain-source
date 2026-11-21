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

# git: the import walker lists files with `git ls-files` (src/commands/import.ts); tini: PID 1 reaps the
# children `serve` and the cron jobs spawn; ca-certificates: OpenAI / DeepInfra TLS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

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
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/gbrain-entrypoint"]
CMD ["serve", "--http", "--port", "8199", "--bind", "0.0.0.0"]
