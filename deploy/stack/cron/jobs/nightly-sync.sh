#!/bin/bash
# jobs/nightly-sync.sh: ingest new/changed brain-repo files into gbrain, then extract links and commit the brain
# repo snapshot (card t_ab2fcba3). The container port of hermes-home scripts/gbrain-nightly-sync.sh (cron row
# 9b1a4fdaaa2a); the same roots, freshness check, audit-lock skip counter and chunked import.
# What stays on the host: the workflow-observer export (hermes-home scripts/workflow-observer/gbrain-sync.py)
# and hermes-sessions-export write INTO the brain repo from hermes data; they are producers, not this job, and
# keep running there before 03:30 (the cutover runbook lists the row edit).
# What is gone: the launchctl bootout/bootstrap PGLite dance (engine=postgres since 2026-07-09; dead code on host).
# Paths: the brain repo is bind-mounted at its HOST path ($GBRAIN_HOME/brain, e.g. /Users/alexgierczyk/gbrain/brain):
# gbrain records the imported root's absolute path in ingest_log.source_ref and ingest-chunked's readback compares
# against it, so the container must import from the same path the launchd job did. State lives on /state (stamp,
# skip counter, audit lock, logs).
# Exit 0 green or nothing new; 1 failure (gbrainjob records it; the host watch pages).
set -euo pipefail
GB="${GBRAIN_HOME:?GBRAIN_HOME unset}"
BRAIN="$GB/brain"
ST="${GBRAIN_STATE_DIR:-/state}"
LIB=/opt/gbrain-cron/lib
LOG="$ST/nightly-sync.log"
STAMP="$ST/last-nightly-sync"
SKIPS_FILE="$ST/nightly-skips"
AUDIT_LOCK="$ST/.audit-lock"
ROOTS="${GBRAIN_NIGHTLY_ROOTS:-vault x-bookmarks personal entities topics media/by-channel media/collections media/non-video media/by-creator media/books}"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

fail() { echo "gbrain-nightly-sync FAILED: $1"; exit 1; }
[ -d "$BRAIN" ] || fail "brain repo not mounted at $BRAIN"

skips_read() { local n; n=$(cat "$SKIPS_FILE" 2>/dev/null || true); case "$n" in ''|*[!0-9]*) n=0 ;; esac; echo "$n"; }
skips_write() { echo "$1" > "$SKIPS_FILE.tmp.$$" && mv -f "$SKIPS_FILE.tmp.$$" "$SKIPS_FILE"; }
AUDIT_LOCK_HELD=0
release_audit_lock() {
  [ "$AUDIT_LOCK_HELD" = 1 ] && python3 "$LIB/audit_lock.py" release --path "$AUDIT_LOCK" --pid "$$" --quiet --ok-if-missing 2>>"$LOG" || true
}

# Anything new under an import root since the last successful run?
NEW=""
for R in $ROOTS; do
  if [ -d "$BRAIN/$R" ]; then
    NEW=$(find "$BRAIN/$R" -name "*.md" -newer "$STAMP" 2>/dev/null | head -1 || true)
    [ -n "$NEW" ] && break
  fi
done
if [ "$DRY_RUN" = 0 ] && [ -f "$STAMP" ] && [ -z "$NEW" ]; then
  skips_write 0
  echo 'EFFECT {"nothing_new": 1}'
  exit 0
fi

rc=0
python3 "$LIB/audit_lock.py" acquire --path "$AUDIT_LOCK" --pid "$$" --owner nightly-sync --quiet 2>>"$LOG" || rc=$?
if [ "$rc" = 75 ]; then
  n=$(( $(skips_read) + 1 )); skips_write "$n" || fail "skipped (audit lock held) and could not record the skip"
  echo "gbrain-nightly-sync skipped: audit lock held at $AUDIT_LOCK (consecutive skips: $n)"
  [ "$n" -ge 3 ] && fail "skipped $n nights in a row (audit lock held at $AUDIT_LOCK); the brain is not being ingested"
  echo "EFFECT {\"skipped_audit_lock\": $n}"
  exit 0
elif [ "$rc" != 0 ]; then
  fail "audit lock acquire rc=$rc"
fi
AUDIT_LOCK_HELD=1
trap release_audit_lock EXIT
skips_write 0

INGEST_PID=""
forward() { [ -n "$INGEST_PID" ] && kill -TERM "$INGEST_PID" 2>/dev/null; wait "$INGEST_PID" 2>/dev/null || true; fail "terminated by $1"; }
trap 'forward TERM' TERM
trap 'forward INT' INT

n_roots=0
for R in $ROOTS; do
  [ -d "$BRAIN/$R" ] || continue
  if [ "$DRY_RUN" = 1 ]; then echo "DRY-RUN: would import $BRAIN/$R"; continue; fi
  irc=0
  GBRAIN_DIR="$GB" GBRAIN_CMD=/usr/local/bin/gbrain GBRAIN_PSQL_DIRECT=1 INGEST_LOG="$ST/ingest-chunk.log" \
    bash "$LIB/ingest-chunked.sh" "$BRAIN/$R" >/dev/null 2>>"$LOG" &
  INGEST_PID=$!
  wait "$INGEST_PID" || irc=$?
  INGEST_PID=""
  [ "$irc" = 0 ] || fail "import $R rc=$irc (see $LOG and $ST/ingest-chunk.log)"
  n_roots=$((n_roots + 1))
done

if [ "$DRY_RUN" = 1 ]; then
  echo "DRY-RUN: would extract links, touch $STAMP, and commit the brain snapshot if changed"
  exit 0
fi
/usr/local/bin/gbrain extract-links >>"$LOG" 2>&1 || echo "gbrain-nightly-sync: extract-links failed (non-fatal)" >>"$LOG"
touch "$STAMP"
committed=0
if git -C "$BRAIN" add -A 2>>"$LOG" && ! git -C "$BRAIN" diff --cached --quiet 2>/dev/null; then
  git -C "$BRAIN" -c user.name="gbrain-cron" -c user.email="gbrain-cron@localhost" \
    commit -q -m "nightly sync: $(date +%F) incremental ingest" 2>>"$LOG" && committed=1
fi
echo "EFFECT {\"roots_imported\": $n_roots, \"brain_committed\": $committed}"
