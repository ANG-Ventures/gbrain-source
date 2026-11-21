#!/bin/sh
# gbrain container entrypoint (card t_ab2fcba3): the container twin of the old ~/gbrain/gbrain launcher.
#
# Secrets arrive as files under /run/secrets (compose `secrets:`, written by `gbrainctl render` from 1Password);
# nothing secret is in the image, the env file or argv. Same posture as the launcher:
#   OPENAI_API_KEY      embeddings (required)
#   ANTHROPIC_API_KEY   optional: Anthropic traffic goes to the claude-apr relay at ANTHROPIC_BASE_URL
#   DATABASE_URL        engine=postgres; built from the pg password file + GBRAIN_PG_HOST/PORT
#   GBRAIN_HOME=/gbrain, config.json copied in from the rendered file, self-upgrade/shell jobs/contributor OFF.
# Exec's `bun /app/src/cli.ts "$@"` (default CMD: serve --http --port 8199 --bind 0.0.0.0).
set -eu
die() { echo "FATAL: gbrain entrypoint: $*" >&2; exit 1; }
S="${GBRAIN_SECRETS_DIR:-/run/secrets}"

[ -s "$S/openai-key" ] || die "missing $S/openai-key (run gbrainctl render)"
OPENAI_API_KEY="$(cat "$S/openai-key")"; export OPENAI_API_KEY
if [ -s "$S/anthropic-key" ]; then
  ANTHROPIC_API_KEY="$(cat "$S/anthropic-key")"; export ANTHROPIC_API_KEY
fi
[ -s "$S/pg-password" ] || die "missing $S/pg-password (run gbrainctl render)"
PGP="$(bun -e 'process.stdout.write(encodeURIComponent(require("fs").readFileSync(process.argv[1],"utf8").replace(/\n$/,"")))' "$S/pg-password")" \
  || die "could not read $S/pg-password"
DATABASE_URL="postgresql://${GBRAIN_PG_USER:-gbrain}:${PGP}@${GBRAIN_PG_HOST:-postgres}:${GBRAIN_PG_PORT:-5432}/${GBRAIN_PG_DB:-gbrain}"
export DATABASE_URL
unset PGP

mkdir -p "$GBRAIN_HOME/.gbrain"
[ -s "$S/config.json" ] || die "missing $S/config.json (run gbrainctl render)"
cp "$S/config.json" "$GBRAIN_HOME/.gbrain/config.json"
chmod 600 "$GBRAIN_HOME/.gbrain/config.json"

unset GBRAIN_ALLOW_SHELL_JOBS GBRAIN_CONTRIBUTOR_MODE 2>/dev/null || true
exec bun /app/src/cli.ts "$@"
