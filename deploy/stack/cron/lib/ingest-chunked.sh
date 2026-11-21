#!/usr/bin/env bash
# Chunked ingest driver: run `gbrain import`, kill if the log stalls, rerun.
# CONTAINER COPY (deploy/stack/cron/lib, card t_ab2fcba3) of ANG-Ventures/gbrain ingest-chunked.sh @335cb5d
# (sha256 12732e8e...); the one change is readback_psql (GBRAIN_PSQL_DIRECT=1), so the readback works without
# a docker socket. Behaviour with the variable unset is the host original's.
#
# 2026-07-28 STALL THRESHOLD 180 -> 2400s. The watchdog keys off LOG MTIME, but
# a single large file emits NOTHING while it is being chunked, so "log is quiet"
# conflates WEDGED with WORKING-HARD. Measured on this corpus, chunk time is
# superlinear in PARAGRAPH COUNT (fit: seconds ~= 5.06e-3 * paras^1.14):
#     5k paras ~ 1.4min | 20k ~ 6.7min | 40k ~ 15min | 78k ~ 31min
# 117 files in brain/media/books exceed 180s of silent work, so the old
# threshold KILLED REAL PROGRESS and retried the same file every pass -- the
# job could never converge (observed: 8 passes, same file, 30min, 0 imported).
# 2400s clears the measured worst case (~32min) with margin. A genuinely hung
# import still gets killed, just not a merely-slow one.
# Import is incremental (already-imported files skip), so repeated runs converge.
#
# 2026-10-15 hardening (t_bab3e569):
#  G1  TERM/INT/HUP to this wrapper kills the import child (it used to finish
#      orphaned while a retry raced it), and a pass refuses to start (exit 75)
#      while another `cli.ts import <same root>` is running.
#  G3  the watchdog's own `sleep` is a reaped background job, so it is never
#      reported as "line 24: NNNN Killed: 9  sleep 30". Real actions log "WATCHDOG:".
#      The stall kill now targets THIS pass's import only (was: pkill every import).
#  G4  completion = DB readback when engine=postgres: every importable file under
#      the root (the app walker's rules: git ls-files, no dot/pruned dirs, no
#      metafiles, no bracket names) needs a live pages.source_path row (compared
#      lowercased + NFC; slugs are relative to the imported dir, so there is no
#      root-slug prefix to match). Missing > 0.5% -> nonzero exit with both numbers.
#      Root provenance (t_32a11abe): a row whose latest ingest_log writer is ANOTHER
#      root holding different content at that path does not count for this root.
#      The `[import.files] N/N` log line is kept as context only.
#  Exit: 0 complete | 1 incomplete/unverified | 75 same root already importing
#        | 143 terminated by signal.
# Usage: ingest-chunked.sh [target-dir]   (default ./brain/vault)
# Test seams (env): GBRAIN_DIR GBRAIN_CMD GBRAIN_DOCKER INGEST_LOG STALL_LIMIT_S
#   WATCHDOG_POLL_S PASS_GAP_S MAX_PASSES INGEST_MISSING_PERMILLE INGEST_CHUNKED_SOURCE_ONLY
set -u
# The extension list shared with jobs/nightly-sync.sh's freshness gate (one list: lib/import-exts.sh).
. "$(dirname "$0")/import-exts.sh"
export GBRAIN_IMPORT_EXTS
GB="${GBRAIN_DIR:-$HOME/gbrain}"
GBRAIN_CMD="${GBRAIN_CMD:-$GB/gbrain}"
DOCKER="${GBRAIN_DOCKER:-}"
if [ -z "$DOCKER" ]; then
  if [ -x /usr/local/bin/docker ]; then DOCKER=/usr/local/bin/docker; else DOCKER=docker; fi
fi
LOG="${INGEST_LOG:-$GB/ingest-chunk.log}"
POLL_S="${WATCHDOG_POLL_S:-30}"
PASS_GAP_S="${PASS_GAP_S:-10}"
MAX_PASSES="${MAX_PASSES:-8}"
MISSING_PERMILLE="${INGEST_MISSING_PERMILLE:-5}"   # 5 permille = 0.5%

PID=""
SLEEP_PID=""
TARGET=""

log() { echo "$*" >> "$LOG"; }

# Absolute, symlink-free form of a directory (empty if it does not exist).
abs_dir() { (cd "$1" 2>/dev/null && pwd -P); }

# PIDs of running `cli.ts import <root>` processes for this root: the argument
# as given, its absolute path, or its ./-relative form under $GB.
same_root_pids() {
  local target="$1" abs gbabs rel=""
  abs="$(abs_dir "$target")"
  gbabs="$(abs_dir "$GB")"
  case "$abs" in "$gbabs"/*) rel="./${abs#"$gbabs"/}" ;; esac
  ps -axo pid=,command= | awk -v t="$target" -v a="$abs" -v r="$rel" -v self="$$" '
    function hit(cmd, root,   k, rest) {
      if (root == "") return 0
      k = index(cmd, "cli.ts import " root)
      if (k == 0) return 0
      rest = substr(cmd, k + length("cli.ts import " root))
      return (rest == "" || rest ~ /^\/?( |$)/)
    }
    { pid = $1; cmd = $0; sub(/^ *[0-9]+ /, "", cmd)
      if (pid == self) next
      if (hit(cmd, t) || hit(cmd, a) || hit(cmd, r)) print pid }'
}

# Import-eligible files under a root, one per line, lowercased + NFC, COPY-escaped.
# Mirrors app/src/commands/import.ts collectSyncableFiles (markdown strategy).
# With a second arg (a file of "path<TAB>writer-root" lines from readback) it
# instead prints "present foreign": a row is FOREIGN when its latest ingest_log
# writer is a different root holding different bytes at the same normalized path.
expected_list() {
  python3 - "$1" "${2:-}" "$GB" <<'PY'
import os, subprocess, sys, unicodedata
root, claims, gb = sys.argv[1], sys.argv[2], sys.argv[3]
PRUNE = {"node_modules", "vendor", "dist", "build", "venv", "ops"}
SKIP = {"schema.md", "index.md", "log.md", "README.md", "RESOLVER.md"}
EXTS = tuple("." + e for e in os.environ["GBRAIN_IMPORT_EXTS"].split())   # lib/import-exts.sh
def ok(rel):
    segs = rel.split("/")
    if any(s in PRUNE or s.endswith(".raw") or s.startswith(".") for s in segs):
        return False
    if any(ord(c) < 32 for c in rel) or not rel.endswith(EXTS):
        return False
    if "[" in rel or "]" in rel or segs[-1] in SKIP:
        return False
    p = os.path.join(root, rel)
    return os.path.isfile(p) and not os.path.islink(p)
def list_rels(root):
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "--cached", "--others",
                              "--exclude-standard", "-z"], capture_output=True, check=True).stdout
        return [r for r in out.decode("utf-8", "surrogateescape").split("\0") if r]
    except Exception:
        rels = []
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if not x.startswith(".") and x not in PRUNE and not x.endswith(".raw")]
            rels.extend(os.path.relpath(os.path.join(d, f), root) for f in files)
        return rels
rels = list_rels(root)
key = lambda r: unicodedata.normalize("NFC", r).lower()
if not claims:
    for r in rels:
        if ok(r):
            print(key(r).replace("\\", "\\\\").replace("\t", "\\t"))
    sys.exit(0)
orig = {key(r): r for r in rels if ok(r)}
here = os.path.realpath(root)
import filecmp, re
unesc = lambda s: re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n", "r": "\r"}.get(m.group(1), m.group(1)), s)
index = {}
def spellings(other, p):
    # The other root's files whose NORMALIZED path (the DB match key) equals p, in
    # their on-disk spelling: `Notes.md` in B and `notes.md` in A share one key.
    if other not in index:
        idx = index[other] = {}
        for r in (list_rels(other) if os.path.isdir(other) else []):
            f = os.path.join(other, r)
            if os.path.isfile(f) and not os.path.islink(f):
                idx.setdefault(key(r), []).append(f)
    return index[other].get(p, [])
def own(p, ref):
    if ref in ("", "\\N"):
        return True  # no recorded writer: provenance unknown, keep the row
    other = os.path.realpath(os.path.join(gb, ref))  # refs like ./brain/x are relative to $GB
    if other == here:
        return True
    theirs = spellings(other, p)
    # Foreign = last written from ANOTHER root that still holds a DIFFERENT file at
    # this normalized path, so the row is not this root's content. A vanished root
    # or file (e.g. a deleted /tmp shard) cannot be compared and is kept.
    mine = os.path.join(here, orig[p])
    return not theirs or any(filecmp.cmp(mine, t, shallow=False) for t in theirs)
present, foreign = set(), set()
with open(claims, encoding="utf-8", errors="surrogateescape") as fh:
    for line in fh:
        p, _, ref = line.rstrip("\n").partition("\t")
        p = unesc(p)
        if p not in orig:
            continue
        (present if own(p, unesc(ref)) else foreign).add(p)
print(len(present), len(foreign - present))
PY
}

db_engine() {
  python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('engine','pglite'))" \
    "$GB/.gbrain/config.json" 2>/dev/null || echo unknown
}

# The psql that reads the DB back. Host default: `docker exec gbrain-pg psql`. In the gbrain stack's cron
# container (no docker socket) GBRAIN_PSQL_DIRECT=1 uses the image's psql over the compose network, with the
# connection from PGHOST/PGPORT/PGUSER/PGDATABASE/PGPASSFILE (deploy/stack/cron, card t_ab2fcba3).
readback_psql() {
  if [ "${GBRAIN_PSQL_DIRECT:-0}" = "1" ]; then
    psql -qtAX -v ON_ERROR_STOP=1
  else
    "$DOCKER" exec -i gbrain-pg psql -U gbrain -d gbrain -qtAX -v ON_ERROR_STOP=1
  fi
}

# DB readback. Prints "present expected"; nonzero if the DB could not be read.
# Root provenance: pages are keyed (source_id, slug) with slug relative to the
# imported dir, so two roots holding `notes.md` share one row. A row counts for
# THIS root unless its latest ingest_log writer (same source_id) is another root
# that holds a different file at that path (see expected_list's claims mode).
readback() {
  local target="$1" list expected claims counts present foreign
  list="$(expected_list "$target")" || return 1
  if [ -z "$list" ]; then echo "0 0"; return 0; fi
  expected="$(printf '%s\n' "$list" | wc -l | tr -d ' ')"
  claims="$(mktemp "${TMPDIR:-/tmp}/ingest-claims.XXXXXX")" || return 1
  { printf 'create temp table _exp(p text);\ncopy _exp from stdin;\n'
    printf '%s\n' "$list"
    printf '\\.\n'
    cat <<'SQL'
create temp table _w as select distinct on (l.source_id, s.slug) l.source_id, s.slug, l.source_ref
  from ingest_log l cross join lateral jsonb_array_elements_text(l.pages_updated) s(slug)
 where l.source_type = 'directory' order by l.source_id, s.slug, l.created_at desc, l.id desc;
copy (select e.p, w.source_ref from _exp e
        join pages p on p.deleted_at is null and lower(normalize(p.source_path, NFC)) = e.p
        left join _w w on w.source_id = p.source_id and w.slug = p.slug) to stdout;
select 'END';
SQL
  } | readback_psql >"$claims" 2>>"$LOG" \
    && [ "$(tail -1 "$claims")" = "END" ] || { rm -f "$claims"; return 1; }
  sed -i.bak '$d' "$claims" && rm -f "$claims.bak"
  counts="$(expected_list "$target" "$claims")" || counts=""
  rm -f "$claims"
  present="${counts% *}"; foreign="${counts#* }"
  case "$present" in ''|*[!0-9]*) return 1 ;; esac
  case "$foreign" in ''|*[!0-9]*) return 1 ;; esac
  [ "$foreign" -eq 0 ] || log "READBACK: $foreign path(s) present only as rows last written from another root with different content; not counted for $target"
  echo "$present $expected"
}

# File mtime in epoch seconds. The stat dialect is chosen explicitly: on GNU,
# `stat -f %m` is not an error-only probe (it prints filesystem info for the file
# to stdout), so a `bsd || gnu` fallback chain concatenates junk into the value.
if stat -c %Y / >/dev/null 2>&1; then
  mtime() { stat -c %Y "$1" 2>/dev/null; }
else
  mtime() { stat -f %m "$1" 2>/dev/null; }
fi

reap_sleep() {
  if [ -n "$SLEEP_PID" ]; then
    kill "$SLEEP_PID" 2>/dev/null
    wait "$SLEEP_PID" 2>/dev/null
    SLEEP_PID=""
  fi
}

# G1: the import child dies with the wrapper.
on_signal() {
  local sig="$1" i
  trap - TERM INT HUP
  reap_sleep
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    log "SIGNAL: wrapper got $sig, terminating import pid $PID (root $TARGET)"
    pkill -TERM -P "$PID" 2>/dev/null
    kill -TERM "$PID" 2>/dev/null
    for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
    if kill -0 "$PID" 2>/dev/null; then
      log "SIGNAL: import pid $PID ignored TERM for ${i}s, sending KILL"
      pkill -KILL -P "$PID" 2>/dev/null
      kill -KILL "$PID" 2>/dev/null
    fi
    wait "$PID" 2>/dev/null
  else
    log "SIGNAL: wrapper got $sig, no import running"
  fi
  echo "ingest-chunked: terminated by $sig (root $TARGET)" >&2
  exit 143
}

pause() {  # quiet, signal-interruptible sleep
  sleep "$1" & SLEEP_PID=$!
  wait "$SLEEP_PID" 2>/dev/null
  SLEEP_PID=""
}

main() {
  cd "$GB" || { echo "ingest-chunked: cannot cd to $GB" >&2; exit 1; }
  TARGET="${1:-./brain/vault}"
  [ -d "$TARGET" ] || { echo "ingest-chunked: target is not a directory: $TARGET" >&2; exit 1; }
  trap 'on_signal TERM' TERM
  trap 'on_signal INT' INT
  trap 'on_signal HUP' HUP
  : > "$LOG"
  local engine total_t0 pass rc=0 now mt stalled others rb="" present expected missing
  engine="$(db_engine)"
  total_t0=$(date +%s)
  for pass in $(seq 1 "$MAX_PASSES"); do
    others="$(same_root_pids "$TARGET" | tr '\n' ' ')"
    if [ -n "${others// /}" ]; then
      log "REFUSED: another import of $TARGET is already running (pid ${others% })"
      echo "ingest-chunked: refusing pass $pass: another 'cli.ts import $TARGET' is already running (pid ${others% }); not starting a second import of the same root" >&2
      exit 75
    fi
    log "=== PASS $pass $(date) ==="
    "$GBRAIN_CMD" import "$TARGET" >> "$LOG" 2>&1 &
    PID=$!
    stalled=0
    while kill -0 "$PID" 2>/dev/null; do
      pause "$POLL_S"
      kill -0 "$PID" 2>/dev/null || break
      now=$(date +%s)
      mt="$(mtime "$LOG")"
      case "$mt" in ''|*[!0-9]*)
        log "WATCHDOG: cannot read mtime of $LOG (got '${mt:0:40}'), skipping stall check this poll"
        continue ;;
      esac
      if [ $((now - mt)) -gt "${STALL_LIMIT_S:-2400}" ]; then
        log "WATCHDOG: stalled $((now-mt))s, killing pass $pass (pid $PID)"
        pkill -KILL -P "$PID" 2>/dev/null
        kill -KILL "$PID" 2>/dev/null
        stalled=1
        break
      fi
    done
    wait "$PID" 2>/dev/null
    rc=$?
    PID=""
    log "=== PASS $pass done rc=$rc ==="
    log "last: $(grep -E '^\[import.files\]' "$LOG" | tail -1)"   # context only, not a completion signal

    if [ "$engine" = "postgres" ]; then
      if rb="$(readback "$TARGET")"; then
        present="${rb% *}"; expected="${rb#* }"
        missing=$((expected - present))
        log "READBACK: $present/$expected importable files present in pages (missing $missing)"
        if [ $((missing * 1000)) -le $((expected * MISSING_PERMILLE)) ]; then
          # Rows present prove the paths were imported once, not that this pass wrote the current contents:
          # a failed or killed pass never completes the root (Prism d41bb2babef3); the next pass re-imports
          # and must exit 0 before the root counts as done.
          if [ "$rc" = "0" ] && [ "$stalled" = "0" ]; then
            log "COMPLETE after pass $pass (DB readback $present/$expected)"
            log "TOTAL WALL: $(( $(date +%s) - total_t0 ))s"
            return 0
          fi
          log "READBACK covers the root but pass $pass ended rc=$rc stalled=$stalled; not complete, running another pass"
        elif [ "$rc" = "0" ] && [ "$stalled" = "0" ]; then
          log "INCOMPLETE: import exited 0 but DB holds $present of $expected files"
          echo "ingest-chunked: import of $TARGET exited 0 but the DB holds only $present of $expected importable files (missing $missing, over the ${MISSING_PERMILLE} permille limit)" >&2
          exit 1
        fi
      else
        rb=""
        if [ "$rc" = "0" ] && [ "$stalled" = "0" ]; then
          log "UNVERIFIED: import exited 0 but DB readback failed"
          echo "ingest-chunked: import of $TARGET exited 0 but the DB readback (docker exec gbrain-pg psql) failed; completion unverified" >&2
          exit 1
        fi
      fi
    elif [ "$rc" = "0" ] && [ "$stalled" = "0" ]; then
      log "COMPLETE (natural exit, engine=$engine, no DB readback) after pass $pass"
      log "TOTAL WALL: $(( $(date +%s) - total_t0 ))s"
      return 0
    fi
    pause "$PASS_GAP_S"
  done
  log "TOTAL WALL: $(( $(date +%s) - total_t0 ))s"
  log "GAVE UP after $MAX_PASSES passes (last rc=$rc)"
  echo "ingest-chunked: $TARGET not complete after $MAX_PASSES passes (last rc=$rc${rb:+, DB readback present/expected $rb}); see $LOG" >&2
  exit 1
}

if [ "${INGEST_CHUNKED_SOURCE_ONLY:-0}" = "1" ]; then
  # shellcheck disable=SC2317  # reached only when executed, not sourced
  return 0 2>/dev/null || true
else
  main "$@"
  exit $?
fi
