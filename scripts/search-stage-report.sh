#!/usr/bin/env bash
# SCHEDULE: manual (an operator runs it after a slow search; nothing schedules it)
#
# search-stage-report.sh: where did the time go on gbrain search/query calls?
# Reads mcp_request_log.stage_timing (migration v147) and prints
#   1. p50/p90/p99/max per stage over the window, and
#   2. every request at or over the threshold with the stage that held it.
#
# Usage: scripts/search-stage-report.sh [WINDOW] [THRESHOLD_MS] [OPS]
#   WINDOW        interval, default "24 hours" (e.g. "90 minutes", "7 days")
#   THRESHOLD_MS  slow-request cut, default 8000
#   OPS           comma list, default "search,query"
# Connection: psql "$DATABASE_URL" when DATABASE_URL is set, otherwise
#   docker exec "${GBRAIN_PG_CONTAINER:-gbrain-pg}" psql -U gbrain -d gbrain
set -euo pipefail

WINDOW="${1:-24 hours}"
THRESHOLD="${2:-8000}"
OPS="${3:-search,query}"

die() { echo "search-stage-report: $*" >&2; exit 2; }
[[ "$WINDOW" =~ ^[0-9]+\ (minute|minutes|hour|hours|day|days)$ ]] || die "WINDOW must look like '24 hours' (got '$WINDOW')"
[[ "$THRESHOLD" =~ ^[0-9]+$ ]] || die "THRESHOLD_MS must be an integer (got '$THRESHOLD')"
[[ "$OPS" =~ ^[a-z_]+(,[a-z_]+)*$ ]] || die "OPS must be a comma list of op names (got '$OPS')"
OPS_SQL="'${OPS//,/\',\'}'"

SQL_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/search-stage-report.sql"
SQL="$(<"$SQL_FILE")"
SQL="${SQL//__WINDOW__/$WINDOW}"
SQL="${SQL//__THRESHOLD_MS__/$THRESHOLD}"
SQL="${SQL//__OPERATIONS__/$OPS_SQL}"
Q1="${SQL%%-- @@*}"
Q2="${SQL#*-- @@}"

echo "== per-stage ms, last $WINDOW, ops $OPS =="
if [ -n "${DATABASE_URL:-}" ]; then
  psql "$DATABASE_URL" -X -v ON_ERROR_STOP=1 -P pager=off -c "$Q1" -c "\\echo == requests >= ${THRESHOLD} ms, top stage ==" -c "$Q2"
else
  docker exec "${GBRAIN_PG_CONTAINER:-gbrain-pg}" psql -U gbrain -d gbrain -X -v ON_ERROR_STOP=1 -P pager=off \
    -c "$Q1" -c "\\echo == requests >= ${THRESHOLD} ms, top stage ==" -c "$Q2"
fi
