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
#   drill.sh f1 <a|b>           read F1: swap to that half, SIGKILL the coordinator once promote_started is journaled,
#                               `swap undo` must refuse (old primary stays fenced), then `swap resume` must finish
#   drill.sh f2 <a|b>           read F2: swap to that half with a failure injected at reseed_old, put the old primary
#                               back by hand, then `swap resume` and a bare `_reseed` must refuse and delete nothing
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
OWN=.gbrain-swapdrill-rig   # written by setup into a rig it made: teardown deletes only a rig that carries it
VOLS="gbrainswapdrill_a_pg gbrainswapdrill_b_pg"   # the drill profiles' pg_volume (external: no compose label)
subnet_of() { docker network inspect "$1_default" -f '{{(index .IPAM.Config 0).Subnet}}'; }

setup() {
  [[ -z $(git -C "$REPO" status --porcelain -- deploy/stack) ]] || { echo "dirty checkout: the halves run the reviewed bytes" >&2; exit 2; }
  sudo -n test -s "$DATA/a/pg/PG_VERSION" || { echo "no copy in $DATA/a/pg (STANDBY.md Drill: seed-copy first)" >&2; exit 2; }
  # both halves' data roots, ours (init makes each brain repo as this user; pg/ is made by a root container)
  sudo -n install -d -o "$(id -u)" -g "$(id -g)" "$DATA" "$DATA/a" "$DATA/b"
  docker image inspect "$SSHD_IMAGE" >/dev/null
  # a private rig (the sshd runs StrictModes no, so its authorized_keys must be ours alone; Prism 2f422479f108):
  # a new rig is made 0700 and marked; an existing one must already be the drill's, then it is made private again
  if [[ -e $RIG && ! -e $RIG/$OWN ]]; then echo "$RIG exists and is not a drill rig (no $OWN): refusing" >&2; exit 2; fi
  (umask 077; mkdir -p "$RIG/sshd" "$RIG/state")
  touch "$RIG/$OWN"
  chmod 700 "$RIG" "$RIG/sshd" "$RIG/state"
  [[ -f $RIG/sshd/ssh_host_ed25519_key ]] || ssh-keygen -q -t ed25519 -N '' -C gbrain-swapdrill-rig -f "$RIG/sshd/ssh_host_ed25519_key"
  local hk; hk=$(cut -d' ' -f1,2 "$RIG/sshd/ssh_host_ed25519_key.pub")
  touch "$RIG/sshd/authorized_keys"; chmod 600 "$RIG/sshd/authorized_keys"
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
# a fresh 0600 file renamed over the old one: an existing file's looser mode never carries over (Prism d3858b8802f0)
tmp = sys.argv[1] + ".tmp"
try:
    os.unlink(tmp)
except FileNotFoundError:
    pass
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, f"GBRAIN_RAIL_CLIENT_ID={cid.group(1)}\nGBRAIN_RAIL_CLIENT_SECRET={sec.group(1)}\n".encode())
os.close(fd)
os.replace(tmp, sys.argv[1])
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
  # the verifier on stdin, never argv (read F5)
  jq -r .verifier <<<"$inp" | ctl swap _primary_enable --host-file "$A" --key-line "$(jq -r .key_line <<<"$inp")" \
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
  local rc=0
  one_swap b gbrain-swapdrill-b-pg || rc=1
  say "after swap 1: the new standby (A)"
  ctl standby status --host-file "$A" || { echo "DRILL: A is not a FRESH, ELIGIBLE standby after swap 1" >&2; rc=1; }
  probe
  one_swap a gbrain-swapdrill-a-pg || rc=1
  say "after swap 2: the standby (B)"
  # the drill passes only on FRESH + ELIGIBLE=yes here (Prism 7d044f6fada7)
  ctl standby status --host-file "$B" || { echo "DRILL: B is not a FRESH, ELIGIBLE standby after swap 2" >&2; rc=1; }
  probe
  echo "DRILL=$([[ $rc == 0 ]] && echo ok || echo failed)"
  return $rc
}

other() { case $1 in a) echo b ;; b) echo a ;; esac; }
newest_swap() { ls -1 "$GBRAIN_SWAP_STATE" 2>/dev/null | grep -v '^\.' | sort | tail -1; }
in_recovery() { docker exec "gbrain-swapdrill-$1-pg" psql -X -At -U gbrain -d postgres -c 'select pg_is_in_recovery()' 2>&1 | tail -1; }

f1() {  # half to promote
  local h=$1 o; o=$(other "$1"); prof "$h" >/dev/null
  local before; before=$(newest_swap)
  say "F1 arm: swap --to-file drill-$h; the coordinator is SIGKILLed once promote_started is journaled"
  # exec: $! must be the coordinator itself, not a subshell around it (its children, the side verbs, live on)
  ( exec python3 "$RIG/home-a/deploy/stack/gbrainctl" swap --to-file "$(prof "$h")" > "$RIG/f1-swap.log" 2>&1 ) &
  local cp=$! sid="" n
  for n in $(seq 3000); do
    sid=$(newest_swap)
    [[ -n $sid && $sid != "$before" ]] && grep -q '"promote_started"' "$GBRAIN_SWAP_STATE/$sid/state.json" 2>/dev/null && break
    sid=""; sleep 0.2
  done
  [[ -n $sid ]] || { kill "$cp"; echo "F1: promote_started never journaled" >&2; return 1; }
  kill -9 "$cp"; wait "$cp" 2>/dev/null || true
  echo "F1 coordinator pid=$cp SIGKILLed; swap=$sid; target $h in_recovery=$(in_recovery "$h") (the remote _promote runs on)"
  local urc=0; ctl swap undo "$sid" || urc=$?
  echo "F1_UNDO_RC=$urc old $o: ROLE=$(cat "$RIG/home-$o/deploy/stack/ROLE" 2>/dev/null) fence=$(jq -r .swap_id \
    "$RIG/home-$o/deploy/stack/FENCE" 2>/dev/null) postgres=$(docker inspect -f '{{.State.Status}}' "gbrain-swapdrill-$o-pg")"
  sleep 20
  echo "F1 target $h in_recovery=$(in_recovery "$h") 20 s later"
  local rrc=0; ctl swap resume "$sid" | tail -4 || rrc=$?
  ctl standby status --host-file "$(prof "$o")" | tail -1 || rrc=1
  echo "F1=$([[ $urc == 3 && $rrc == 0 ]] && echo ok || echo failed) undo_rc=$urc resume_rc=$rrc"
  [[ $urc == 3 && $rrc == 0 ]]
}

f2() {  # half to promote (the one whose profile lists the standby overlay: b, the nas-fleet stand-in)
  local h=$1 o; o=$(other "$1"); prof "$h" >/dev/null
  local os="$RIG/home-$o/deploy/stack" hs="$RIG/home-$h/deploy/stack" pg="gbrain-swapdrill-$o-pg" sid rc=0
  say "F2 arm: swap --to-file drill-$h with a failure injected at reseed_old"
  GBRAIN_SWAP_INJECT_FAIL=reseed_old ctl swap --to-file "$(prof "$h")" | tail -3 || rc=$?
  sid=$(newest_swap); echo "F2 swap=$sid rc=$rc (want 1 at reseed_old)"
  say "F2: $o back as the primary by hand, STANDBY.md \"Promoted but not serving\" B1-B3"
  cx "$(prof "$h")" stop server cron postgres                                                       # B1
  docker ps -a --filter "label=com.docker.compose.project=gbrain-swapdrill-$h" --format '{{.Names}} {{.State}}' \
    | sed 's/^/B1 /'
  rm -f "$hs/ROLE"; cx "$(prof "$h")" up --no-start --no-deps --force-recreate postgres 2>&1 | tail -1    # B2
  echo "B2 $h postgres guarded: $(docker inspect -f '{{json .Config.Cmd}}' "gbrain-swapdrill-$h-pg" | grep -c 'exit 78')"
  ctl swap _unfence --host-file "$(prof "$o")" --swap-id "$sid" | sed -n 's/^SIDE=//p' \
    | jq -c '{ok,pg_in_recovery,health_ok,cron_running,role,error}'                                # B3
  ctl swap _prove --host-file "$(prof "$o")" | sed -n 's/^SIDE=//p' | jq -c '{ok,hits,search_ms}'
  docker exec "$pg" psql -X -q -U gbrain -d gbrain -c "insert into swapdrill_acks(seq) values (-4242)"
  snap() { echo "in_recovery=$(in_recovery "$o") pages=$(docker exec "$pg" psql -X -At -U gbrain -d gbrain \
    -c 'select count(*) from pages') marker=$(docker exec "$pg" psql -X -At -U gbrain -d gbrain \
    -c 'select count(*) from swapdrill_acks where seq = -4242') datadir=$(sudo -n ls -A "$DATA/$o/pg" | sort \
    | tr '\n' ',' | md5sum | cut -c1-12) ROLE=$(cat "$os/ROLE" 2>/dev/null || echo -) FENCE=$(test -e "$os/FENCE" \
    && echo yes || echo -)"; }
  local b; b=$(snap); echo "F2 before: $b"
  local r1=0 r2=0 r3=0
  say "F2: swap resume (journal says reseed_old is next) and the bare _reseed, with and without the swap id"
  ctl swap resume "$sid" | tail -3 || r1=$?
  ctl swap _reseed --host-file "$(prof "$o")" --swap-id "$sid" | sed -n 's/^SIDE=//p' | jq -c '{ok,error}' || r2=$?
  ctl swap _reseed --host-file "$(prof "$o")" 2>&1 | sed -n 's/^SIDE=//p' | jq -c '{ok,error}' || r3=$?
  local a; a=$(snap); echo "F2 after:  $a"
  say "F2: B5, $h back to a standby of $o"
  local t0=$SECONDS r5=0
  ctl standby seed --reseed --host-file "$(prof "$h")" | tail -3 || r5=$?
  echo "B5_RESEED_S=$((SECONDS - t0))"
  ctl standby status --host-file "$(prof "$h")" | tail -1 || r5=1
  say "F2: B4, the journal closed (a new swap is allowed, this one can never be resumed)"
  mv "$GBRAIN_SWAP_STATE/$sid" "$GBRAIN_SWAP_STATE/../state-closed-$sid"
  ctl swap status | tail -1
  ctl swap status | tail -1 | grep -q '^SWAP=status open=-$' || r5=1
  local ok=0; [[ $r1 == 1 && $r2 != 0 && $r3 != 0 && $a == "$b" && $r5 == 0 ]] && ok=1
  echo "F2=$([[ $ok == 1 ]] && echo ok || echo failed) resume_rc=$r1 bare_reseed_rc=$r2 no_id_reseed_rc=$r3" \
    "unchanged=$([[ $a == "$b" ]] && echo yes || echo NO) b5_rc=$r5"
  [[ $ok == 1 ]]
}

teardown() {
  # by compose label, not `gbrainctl compose -- down`: gbrainctl refuses a leading global flag (--profile), so the
  # promoted-profile server/cron would be left behind; containers go first, then volumes, then the data
  local p
  for p in gbrain-swapdrill-b gbrain-swapdrill-a; do
    docker ps -aq --filter "label=com.docker.compose.project=$p" | xargs -r docker rm -f >/dev/null
    docker network rm "${p}_default" >/dev/null 2>&1 || true
  done
  docker rm -f gbrain-swapdrill-sshd >/dev/null 2>&1 || true
  # only the drill's own volumes: the two projects' (by compose label) and the two pg volumes by exact name, never a
  # name prefix (Prism e5e1befcbdfe)
  local v
  for v in $(docker volume ls -q --filter label=com.docker.compose.project=gbrain-swapdrill-a) \
           $(docker volume ls -q --filter label=com.docker.compose.project=gbrain-swapdrill-b) $VOLS; do
    docker volume inspect "$v" >/dev/null 2>&1 && docker volume rm "$v" >/dev/null
  done
  sudo -n rm -rf "$DATA"
  # the rig only when setup made it (Prism 36c5cf4c7e7b)
  if [[ -e $RIG/$OWN ]]; then rm -rf "$RIG"; elif [[ -e $RIG ]]; then echo "$RIG has no $OWN: not a drill rig, left as is" >&2; fi
  local left=0
  for v in $VOLS; do docker volume inspect "$v" >/dev/null 2>&1 && left=$((left + 1)); done
  echo "TEARDOWN=ok left: containers=$(docker ps -a --format '{{.Names}}' | grep -c swapdrill || true) volumes=$left"
}

case ${1:-} in
  setup) setup ;;
  swap) h=$2; shift 2; ctl swap --to-file "$(prof "$h")" "$@" ;;
  ctl) shift; ctl "$@" ;;
  probe) probe ;;
  run) run ;;
  f1) f1 "${2:?half}" ;;
  f2) f2 "${2:?half}" ;;
  teardown) teardown ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
