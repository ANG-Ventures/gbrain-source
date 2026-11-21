#!/bin/sh
# gbrain cron entrypoint (card t_ab2fcba3): prepare /state and GBRAIN_HOME for the job uid (the host uid that owns
# the bind-mounted brain repo, so `git commit` in it keeps the host's ownership), write the libpq password file and
# the ssh config restic's sftp backend uses, then drop root for good and run `gbrainjob serve`.
# The compose secrets are copied to $GBRAIN_SECRETS_DIR (on /state, 0600, job uid): a bind-mounted secret keeps the
# HOST file's owner and mode, which the job uid may not be able to read on every docker runtime.
set -eu
uid="${GBRAIN_CRON_UID:?GBRAIN_CRON_UID unset: run gbrainctl render}"; gid="${GBRAIN_CRON_GID:-$uid}"
S=/run/secrets
st="${GBRAIN_STATE_DIR:-/state}"
mkdir -p "$st/results" "$st/locks" "$st/.ssh" "$GBRAIN_HOME/.gbrain"
umask 077
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
chown -R "$uid:$gid" "$st" "$GBRAIN_HOME/.gbrain"
for d in ${GBRAIN_RECEIPTS_DIR:-} ${GBRAIN_BACKUP_STAMP_DIR:-}; do [ -d "$d" ] && chown "$uid:$gid" "$d" 2>/dev/null || true; done
exec setpriv --reuid="$uid" --regid="$gid" --clear-groups -- python3 /usr/local/bin/gbrainjob serve
