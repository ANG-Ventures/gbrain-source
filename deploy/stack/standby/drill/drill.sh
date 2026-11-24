#!/usr/bin/env bash
# standby/drill/drill.sh: the gbrain standby + swap drill (card t_dd1e1edb, STANDBY.md). Run ON nas-fleet as ace.
# Never production: two compose projects of their own (gbrain-swapdrill-a / -b, ports 1543x / 1829x / 1828x, data
# under /srv/gbrain-standby/drill-t_dd1e1edb), a rig sshd shaped like the Studio's _studiolink (host network,
# 192.168.1.154:22091, ForceCommand false, forwarding only), the reviewed bytes of this checkout (git archive HEAD).
# Half A starts from a pg_basebackup COPY of the live gbrain-pg (put in a/pg beforehand: STANDBY.md "Drill"),
# rendered with this host's read-only 1P token (the copy's own gbrain password), never armed.
#
#   drill.sh setup              A up on the copy (+ a drill OAuth client for prove), rig, B init + A accepts it +
#                               `standby seed` of B through the link, B's doctor
#   drill.sh swap <a|b> [args]  gbrainctl swap --to-file <that half> [args]   (GBRAIN_SWAP_STATE in the run dir)
#   drill.sh ctl <args>         gbrainctl with the drill env, from home-a
#   drill.sh probe              A's and B's loopback /health + one search through each half's serve port
#   drill.sh teardown           both projects, their volumes, the rig, the data dir
set -euo pipefail
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)          # deploy/stack of the checkout this file is in
REPO=$(cd "$SRC/../.." && pwd)
RIG=${GBRAIN_DRILL_RIG:-$HOME/gbrain-swapdrill/run}
DATA=/srv/gbrain-standby/drill-t_dd1e1edb
SSHD_IMAGE=${GBRAIN_DRILL_SSHD_IMAGE:-t-a107-rh-sshd:latest}
RIG_ADDR=192.168.1.154
RIG_PORT=22091
export GBRAIN_SWAP_STATE=$RIG/state
A=$RIG/drill-a.toml B=$RIG/drill-b.toml
ctl() { python3 "$RIG/home-a/deploy/stack/gbrainctl" "$@"; }
cx() { local f=$1; shift; ctl compose --host-file "$f" -- "$@"; }
say() { printf '== %s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
prof() { case $1 in a) echo "$A" ;; b) echo "$B" ;; *) echo "drill.sh: half must be a|b" >&2; exit 2 ;; esac; }
pg_up() {  # container: the FINAL server answers over TCP twice in a row (the fresh-volume initdb race, mem0 t_b24524ad)
  local ok=0
  for _ in $(seq 120); do
    if docker exec "$1" pg_isready -q -h 127.0.0.1 -U gbrain -d gbrain >/dev/null 2>&1; then
      ok=$((ok + 1)); [[ $ok -lt 2 ]] || return 0
    else ok=0; fi
    sleep 2
  done
  echo "drill.sh: $1 never answered over TCP twice in a row" >&2; return 1
}
health_wait() {  # port seconds
  for _ in $(seq "$2"); do curl -fsS -m 3 "http://127.0.0.1:$1/health" 2>/dev/null | grep -q '"engine":"postgres"' && return 0; sleep 1; done
  echo "drill.sh: 127.0.0.1:$1/health never said engine=postgres" >&2; return 1
}
subnet_of() { docker network inspect "$1_default" -f '{{(index .IPAM.Config 0).Subnet}}'; }

setup() {
  [[ -z $(git -C "$REPO" status --porcelain -- deploy/stack) ]] || { echo "dirty checkout: the halves run the reviewed bytes" >&2; exit 2; }
  sudo -n test -s "$DATA/a/pg/PG_VERSION" || { echo "no copy in $DATA/a/pg (STANDBY.md Drill: seed-copy first)" >&2; exit 2; }
  docker image inspect "$SSHD_IMAGE" >/dev/null
  mkdir -p "$RIG/sshd" "$RIG/state"
  [[ -f $RIG/sshd/ssh_host_ed25519_key ]] || ssh-keygen -q -t ed25519 -N '' -C gbrain-swapdrill-rig -f "$RIG/sshd/ssh_host_ed25519_key"
  local hk; hk=$(cut -d' ' -f1,2 "$RIG/sshd/ssh_host_ed25519_key.pub")
  touch "$RIG/sshd/authorized_keys"
  cat > "$RIG/sshd/sshd_config" <<EOF
Port $RIG_PORT
ListenAddress $RIG_ADDR
HostKey /rig/ssh_host_ed25519_key
PidFile none
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
StrictModes no
AllowUsers _studiolink
AuthorizedKeysFile /rig/authorized_keys
AllowTcpForwarding local
AllowAgentForwarding no
X11Forwarding no
PermitTTY no
ForceCommand /bin/false
EOF
  for x in a b; do
    sed -e "s#@RIG@#$RIG#g" -e "s#@HOSTKEY@#$hk#g" "$SRC/standby/drill/drill-$x.toml" > "$RIG/drill-$x.toml"
    rm -rf "$RIG/home-$x"; mkdir -p "$RIG/home-$x" "$RIG/receipts-$x" "$RIG/stamps-$x"
    git -C "$REPO" archive HEAD | tar -x -C "$RIG/home-$x"
  done
  say "render both halves (1P read-only token of this host)"
  for x in a b; do ctl render --host-file "$RIG/drill-$x.toml" | tail -1; done
  say "init both halves (link keys, replication passwords, volumes bound to $DATA/<half>/pg, empty brain repos)"
  for x in a b; do ctl standby init --host-file "$RIG/drill-$x.toml" | tail -1; done

  say "A: postgres up on the copy"
  local t0=$SECONDS
  cx "$A" up -d postgres
  pg_up gbrain-swapdrill-a-pg
  echo "A_PG_UP_S=$((SECONDS - t0)) pages=$(docker exec gbrain-swapdrill-a-pg psql -X -At -U gbrain -d gbrain -c 'select count(*) from pages')"
  say "A: a drill OAuth client for prove (in the copy: it replicates to B)"
  local out; out=$(cx "$A" run --rm --no-deps -T server auth register-client gbrain-swapdrill-prove \
    --grant-types client_credentials --scopes read 2>&1)
  # the secret prints once (auth.ts formatRegisterClientOutput: `Client ID:` / `Client Secret:` lines); straight to a
  # 0600 file, never to this log
  REG_OUT=$out python3 - "$RIG/prove-client.env" <<'PY'
import os, re, sys
t = os.environ["REG_OUT"]
cid = re.search(r"Client ID:\s+(\S+)", t)
sec = re.search(r"Client Secret:\s+(\S+)", t)
if not cid or not sec or sec.group(1).startswith("<"):
    sys.exit("register-client output not understood: " + re.sub(r"Secret:\s+\S+", "Secret: <redacted>", t)[-400:])
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, f"GBRAIN_RAIL_CLIENT_ID={cid.group(1)}\nGBRAIN_RAIL_CLIENT_SECRET={sec.group(1)}\n".encode())
print("prove client written", cid.group(1)[:16] + "...")
PY
  say "A: the full stack (server, rerank, cron NEVER armed)"
  t0=$SECONDS
  cx "$A" up -d
  health_wait 18291 300
  echo "A_HEALTH_S=$((SECONDS - t0))"

  say "rig sshd (host network, $RIG_ADDR:$RIG_PORT)"
  cx "$B" create --no-recreate primary-link >/dev/null      # B's network, so its subnet is known for the key line
  local sa sb; sa=$(subnet_of gbrain-swapdrill-a) sb=$(subnet_of gbrain-swapdrill-b)
  sed -i "s#^link_from .*#link_from        = \"$sa\"#" "$A"
  sed -i "s#^link_from .*#link_from        = \"$sb\"#" "$B"
  echo "link_from a=$sa b=$sb"
  docker rm -f gbrain-swapdrill-sshd >/dev/null 2>&1 || true
  docker run -d --name gbrain-swapdrill-sshd --network host --restart unless-stopped \
    --label ai.hermes.gbrain-drill.holder=t_dd1e1edb \
    -v "$RIG/sshd:/rig:ro" "$SSHD_IMAGE" /usr/sbin/sshd -D -e -f /rig/sshd_config >/dev/null
  sleep 2; docker logs gbrain-swapdrill-sshd 2>&1 | tail -2

  say "B: A accepts it (primary-enable through swap _inputs/_primary_enable), then standby seed"
  local inp; inp=$(ctl swap _inputs --host-file "$B" | sed -n 's/^SIDE=//p')
  ctl swap _primary_enable --host-file "$A" --key-line "$(jq -r .key_line <<<"$inp")" --verifier "$(jq -r .verifier <<<"$inp")" \
    --role "$(jq -r .role <<<"$inp")" --slot "$(jq -r .slot <<<"$inp")" --mark "$(jq -r .mark <<<"$inp")" \
    | sed -n 's/^SIDE=//p' | jq -c .tail
  t0=$SECONDS
  ctl standby seed --host-file "$B" | tail -6
  echo "B_SEED_S=$((SECONDS - t0))"
  ctl standby status --host-file "$B" || true
}

probe() {
  for x in a b; do
    local p; p=$(python3 -c 'import sys,tomllib; print(tomllib.load(open(sys.argv[1],"rb"))["ports"]["serve"])' "$(prof $x)")
    printf '%s serve %s: ' "$x" "$p"; curl -s -m 3 "http://127.0.0.1:$p/health" || echo "(no answer)"; echo
    ctl swap _prove --host-file "$(prof $x)" | sed -n 's/^SIDE=//p' | jq -c '{ok,hits,search_ms,error}' || true
  done
}

one_swap() {  # half-to-promote new-primary-pg-container: callers + writers on, the swap, callers + writers off, summary
  rm -f "$RIG/load.stop" "$RIG/load.jsonl"
  python3 "$SRC/standby/drill/drill_load.py" "$RIG" 18291 gbrain-swapdrill-a-pg,gbrain-swapdrill-b-pg &
  local lp=$! rc=0
  sleep 10
  say "swap --to-file drill-$1"
  local t0=$SECONDS
  ctl swap --to-file "$(prof "$1")" || rc=$?
  echo "SWAP_WALL_S=$((SECONDS - t0)) rc=$rc"
  sleep 10
  touch "$RIG/load.stop"; wait "$lp" || true
  python3 "$SRC/standby/drill/drill_load.py" summary "$RIG" "$2" || rc=1
  return $rc
}

run() {  # the whole drill after setup: swap to B under load, swap back to A under load, the doctor of each standby
  say "acked-write table on A (replicates)"
  docker exec gbrain-swapdrill-a-pg psql -X -q -U gbrain -d gbrain -c \
    "create table if not exists swapdrill_acks(id bigserial primary key, seq int, at timestamptz default now())"
  sleep 3
  ctl standby status --host-file "$B" | tail -1
  one_swap b gbrain-swapdrill-b-pg
  say "after swap 1: the new standby (A)"
  ctl standby status --host-file "$A" || true
  probe
  one_swap a gbrain-swapdrill-a-pg
  say "after swap 2: the standby (B)"
  ctl standby status --host-file "$B" || true
  probe
}

teardown() {
  for x in b a; do
    [[ -f $(prof $x) ]] && { cx "$(prof $x)" --profile promoted down --remove-orphans -t 30 >/dev/null 2>&1 || true; }
  done
  docker rm -f gbrain-swapdrill-sshd >/dev/null 2>&1 || true
  docker volume rm -f gbrainswapdrill_a_pg gbrainswapdrill_b_pg gbrain-swapdrill-a_cron-state gbrain-swapdrill-a_gbrain-home \
    gbrain-swapdrill-b_cron-state gbrain-swapdrill-b_gbrain-home >/dev/null 2>&1 || true
  sudo -n rm -rf "$DATA"
  rm -rf "$RIG"
  echo "TEARDOWN=ok"
}

case ${1:-} in
  setup) setup ;;
  swap) h=$2; shift 2; ctl swap --to-file "$(prof "$h")" "$@" ;;
  ctl) shift; ctl "$@" ;;
  probe) probe ;;
  run) run ;;
  teardown) teardown ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
