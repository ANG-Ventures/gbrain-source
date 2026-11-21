#!/bin/bash
# jobs/pg-backup.sh: nightly backup of the gbrain Postgres into the restic repo on the NAS (card t_ab2fcba3).
# The container port of hermes-home scripts/gbrain-pg-backup.sh (cron row a3bcde3fa92e, t_d1192ae9): pg_dump -Fp
# streams straight into `restic backup --stdin-from-command` (no local dump file); same repo, same tags
# (gbrain-pg, dump-<date>), same file name (/gbrain-pg.sql), same --host (GBRAIN_RESTIC_HOST = mac-studio-m3u, so
# restic's forget groups and gbrain-offhost-restore-drill keep seeing one series), same 14 daily + 8 weekly
# retention, prune on Sundays, same failed-run cleanup (forget the partial snapshot, repair index).
# Differences from the host script: pg_dump is the image's PG16 client over the compose network (never docker
# exec), the password is the rendered pgpass, and there is no 20-minute "container up?" wait (compose starts this
# container only after postgres is healthy; a down DB fails the dump and the result line says so).
# Env: GBRAIN_RESTIC_REPO, GBRAIN_RESTIC_HOST, RESTIC_PASSWORD_FILE, PGHOST/PGPORT/PGUSER/PGDATABASE/PGPASSFILE,
#      GBRAIN_BACKUP_STAMP_DIR (host bind; last-success-gbrain-pg-backup, read by the fleet backup freshness check).
set -euo pipefail
ST="${GBRAIN_STATE_DIR:-/state}"
RUN_LOG="$ST/pg-backup.log"
logline() { echo "$(date '+%Y-%m-%dT%H:%M:%S%z') $*" >> "$RUN_LOG"; }
export RESTIC_REPOSITORY="${GBRAIN_RESTIC_REPO:?GBRAIN_RESTIC_REPO unset}"
export RESTIC_PASSWORD_FILE="${RESTIC_PASSWORD_FILE:-${GBRAIN_SECRETS_DIR:-/state/secrets}/restic-password}"
export GOMAXPROCS="${GOMAXPROCS:-4}"
RHOST="${GBRAIN_RESTIC_HOST:?GBRAIN_RESTIC_HOST unset}"
STEP_TIMEOUT="${GBRAIN_PG_BACKUP_TIMEOUT:-4h}"
[ -s "$RESTIC_PASSWORD_FILE" ] || { echo "gbrain-pg-backup FAILED: restic password file missing"; exit 1; }
if ! restic cat config >/dev/null 2>&1; then
  if [ "${GBRAIN_RESTIC_INIT_OK:-0}" = 1 ]; then
    restic init >/dev/null 2>&1 || { echo "gbrain-pg-backup FAILED: restic init failed at $RESTIC_REPOSITORY"; exit 1; }
  else
    # Production never inits: an unreachable NAS must page, not silently start a second, empty repo.
    echo "gbrain-pg-backup FAILED: restic repo unreadable at $RESTIC_REPOSITORY"; exit 1
  fi
fi

DUMP_DATE=$(date +%Y%m%d)
SNAP=""
drop_snap() {
  [ -n "${SNAP:-}" ] && [ "$SNAP" != - ] && restic forget --retry-lock 15m "$SNAP" >/dev/null 2>&1 || true
  nice -n 19 timeout -k 1m 1h restic --retry-lock 15m repair index >/dev/null 2>&1 \
    || logline "restic repair index failed after a failed dump; the next run re-uploads what this one wrote"
}
rc=0
OUT=$(PGCONNECT_TIMEOUT=15 nice -n 19 timeout -k 2m "$STEP_TIMEOUT" restic backup --json --retry-lock 15m \
  --host "$RHOST" --tag gbrain-pg --tag "dump-$DUMP_DATE" --stdin-filename gbrain-pg.sql \
  --stdin-from-command -- pg_dump -Fp 2>&1) || rc=$?
SUMMARY=$(printf '%s\n' "$OUT" | grep '"message_type":"summary"' | tail -1) || true
read -r SNAP PROC ADDED < <(printf '%s' "$SUMMARY" | python3 -c '
import json, sys
s = json.loads(sys.stdin.read() or "{}")
print(s.get("snapshot_id", "") or "-", s.get("total_bytes_processed", 0), s.get("data_added", 0))') || true
case "$rc" in
  0) ;;
  124|137) drop_snap; echo "gbrain-pg-backup FAILED: dump into restic WEDGED, killed after $STEP_TIMEOUT (rc $rc)"; exit 1 ;;
  *) drop_snap; echo "gbrain-pg-backup FAILED: dump into restic failed (rc $rc): $(printf '%s' "$OUT" | grep -E '"message_type":"(error|exit_error)"' | tail -2 | tr '\n' ' ' | cut -c1-400)"; exit 1 ;;
esac
SNAP8="${SNAP:0:8}"
[ "${PROC:-0}" -gt 1000000 ] 2>/dev/null || { drop_snap; echo "gbrain-pg-backup FAILED: dump suspiciously small (${PROC:-?} B, snapshot $SNAP8 dropped)"; exit 1; }
# A wrong, emptied or truncated DB still passes the 1 MB floor: compare with the last good run's processed bytes
# (the stamp below). No stamp, or no processed= in it, is the first run: the floor alone applies.
# A deliberate big delete: run it once by hand with GBRAIN_PG_BACKUP_MIN_PCT=0 to move the baseline.
MIN_PCT="${GBRAIN_PG_BACKUP_MIN_PCT:-80}"
S="${GBRAIN_BACKUP_STAMP_DIR:-}/last-success-gbrain-pg-backup"
PREV=$(sed -n 's/.* processed=\([0-9][0-9]*\).*/\1/p' "$S" 2>/dev/null | head -1) || true
if [ -n "${GBRAIN_BACKUP_STAMP_DIR:-}" ] && [ -n "$PREV" ] && [ "$((PROC * 100))" -lt "$((PREV * MIN_PCT))" ]; then
  drop_snap; echo "gbrain-pg-backup FAILED: dump shrank to $PROC B, below $MIN_PCT% of the last good run's $PREV B (snapshot $SNAP8 dropped; deliberate? rerun with GBRAIN_PG_BACKUP_MIN_PCT=0)"; exit 1
fi
logline "dumped -> restic snapshot $SNAP8 processed=$PROC added=$ADDED"
if [ -n "${GBRAIN_BACKUP_STAMP_DIR:-}" ] && [ -d "$GBRAIN_BACKUP_STAMP_DIR" ]; then
  echo "$(date +%s) snapshot=$SNAP8 processed=$PROC added=$ADDED" > "$S.tmp" && mv "$S.tmp" "$S"
fi

FORGET=(forget --tag gbrain-pg --keep-daily 14 --keep-weekly 8)
[ "$(date +%u)" = 7 ] && FORGET+=(--prune)
restic unlock >/dev/null 2>&1 || true
nice -n 19 restic --retry-lock 15m "${FORGET[@]}" >/dev/null 2>&1 || { echo "gbrain-pg-backup FAILED: restic forget failed (tonight's snapshot $SNAP8 is saved)"; exit 1; }  # bash32-array-ok: container bash 5, literal non-empty array
echo "EFFECT {\"snapshot\": \"$SNAP8\", \"bytes_processed\": $PROC, \"bytes_added\": $ADDED}"
