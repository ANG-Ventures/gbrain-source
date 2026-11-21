#!/bin/sh
# gbrain cron entrypoint (card t_ab2fcba3): prepare /state for the jobs, write the libpq password file and the ssh
# config restic's sftp backend uses, then run `gbrainjob serve`.
# Runs as the image's non-root user (hosts/<h>.toml stack_uid: the host uid that owns the bind-mounted brain repo, so
# `git commit` in it keeps the host's ownership). There is no root step: everything written here is in /state (the
# cron-state volume, owned by this uid) or the <gbrain_home>/.gbrain tmpfs (owned by this uid), so no write crosses a
# privilege boundary. The compose secrets are copied to $GBRAIN_SECRETS_DIR (on /state, 0600) so every job reads one
# path whatever mode the runtime presents a bind-mounted secret with.
set -eu
S=/run/secrets
st="${GBRAIN_STATE_DIR:-/state}"
umask 077
mkdir -p "$st/results" "$st/locks" "$st/.ssh" "$GBRAIN_HOME/.gbrain"
SD="${GBRAIN_SECRETS_DIR:?GBRAIN_SECRETS_DIR unset}"
mkdir -p "$SD"
for f in "$S"/*; do [ -f "$f" ] && cp "$f" "$SD/$(basename "$f")"; done
printf '%s:%s:%s:%s:%s\n' "${PGHOST:-postgres}" "${PGPORT:-5432}" "${PGDATABASE:-gbrain}" "${PGUSER:-gbrain}" \
  "$(cat "$SD/pg-password")" > "$st/.pgpass"
if [ -s "$SD/nas-ssh-key" ]; then
  cat > "$st/.ssh/config" <<CFG
Host *
  IdentityFile $SD/nas-ssh-key
  IdentitiesOnly yes
  UserKnownHostsFile $SD/nas-known-hosts
  StrictHostKeyChecking yes
  BatchMode yes
  ServerAliveInterval 30
CFG
fi
cp "$SD/config.json" "$GBRAIN_HOME/.gbrain/config.json"
exec python3 /usr/local/bin/gbrainjob serve
