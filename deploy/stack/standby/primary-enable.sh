#!/usr/bin/env bash
# deploy/stack/standby/primary-enable.sh: the ONE primary-side step of the gbrain warm standby (card t_dd1e1edb,
# STANDBY.md). The port of mem0-selfhost-deploy ops/standby/primary-enable.sh (same verbs, checks and readbacks); the
# differences are named inline. The OPERATOR runs it ON the primary (the Studio), after reading the PR; `gbrainctl
# swap` runs `apply` on whichever host it promotes (the reverse direction). It is ONLINE: no container is recreated or
# restarted, nothing is published on the LAN.
#
#   primary-enable.sh check            read-only: what is in place now (role, hba line loaded, cap, key line, slot)
#   primary-enable.sh apply            with the four STANDBY_* values `gbrainctl standby init` printed on the standby
#   primary-enable.sh undo             the rollback: removes everything apply added, and drops the slot
#
# apply does, in order, each read back before the next:
#   1. CREATE ROLE <role> LOGIN REPLICATION PASSWORD '<SCRAM verifier>' (the password never leaves the standby) and
#      GRANT pg_monitor (the doctor reads pg_stat_replication / pg_replication_slots through it).
#   2. ALTER SYSTEM SET max_slot_wal_keep_size = '8GB' + pg_reload_conf(): a standby that is down can pin at most 8 GB
#      of WAL on the primary (gbrain writes ~1.4 GB of WAL a day, pg_stat_wal 2026-11-24: about 5 days of outage);
#      past that its slot is invalidated (the doctor says "reseed") instead of the disk filling.
#   3. one pg_hba line: `host replication <role> <gw>/32 scram-sha-256`, where <gw> is the gateway of the gbrain
#      postgres container's compose network (gbrain_default 192.168.172.1 on the Studio, read 2026-11-24: a client
#      on the Studio's 127.0.0.1:5433 arrives FROM it, so the NAS address can never appear in pg_hba). The NAS-only
#      gate is the ssh key line in step 4. gbrain-pg's pg_hba has `host all all all scram-sha-256` but no
#      replication line for a non-loopback source, so without this line the base backup is refused.
#      pg_reload_conf(), then pg_hba_file_rules must show the line with no error, else the file is restored, exit 1.
#   4. the key line in the link sshd's keys file (sudo), tagged `# gbrain-standby <mark>`. sshd reads the file per
#      connection: no reload. On the Studio the file is generated elsewhere (claude-pool studio_link_endpoints.py);
#      if a regeneration drops the line, the link goes down and the standby doctor pages STALE.
# Inputs (env, all non-secret): STANDBY_KEY_LINE STANDBY_REPL_VERIFIER STANDBY_REPL_ROLE STANDBY_SLOT.
# Optional: GBRAIN_PG_CONTAINER (default gbrain-pg), STANDBY_KEYS (default /etc/ssh/studiolink_authorized_keys),
# STANDBY_MARK (default `# gbrain-standby t_dd1e1edb`).
set -euo pipefail
PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:$HOME/.orbstack/bin"
verb=${1:-check}
MARK=${STANDBY_MARK:-"# gbrain-standby t_dd1e1edb"}
[[ $MARK =~ ^#\ gbrain-standby\ [A-Za-z0-9_]+$ ]] || { echo "bad STANDBY_MARK" >&2; exit 2; }
KEYS=${STANDBY_KEYS:-/etc/ssh/studiolink_authorized_keys}
role=${STANDBY_REPL_ROLE:-gbrain_repl}
slot=${STANDBY_SLOT:-gbrain_standby_nas_fleet}
[[ $role =~ ^[a-z_][a-z0-9_]*$ && $slot =~ ^[a-z_][a-z0-9_]*$ ]] || { echo "bad role/slot name" >&2; exit 2; }
pgc=${GBRAIN_PG_CONTAINER:-gbrain-pg}
[[ $(docker inspect -f '{{.State.Running}}' "$pgc" 2>/dev/null) == true ]] || { echo "postgres container $pgc is not running" >&2; exit 2; }
# the compose project network (gbrain: gbrain_default); a standby overlay adds no network of its own
net=$(docker inspect "$pgc" -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | tr ' ' '\n' | grep -E '_default$' | head -1)
[[ -n $net ]] || { echo "cannot find the compose network of $pgc" >&2; exit 2; }
gw=$(docker network inspect "$net" -f '{{(index .IPAM.Config 0).Gateway}}')
[[ $gw =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "cannot read the $net gateway" >&2; exit 2; }
HBA="host replication $role $gw/32 scram-sha-256 $MARK"
psql_() { docker exec -i "$pgc" sh -c 'exec psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres' ; }

ROLE_MARK=${MARK#\# }
keys_mark_state() {  # prints present | ABSENT | UNREADABLE:<rc>  (a read and a match are separate steps: a refused
  local body rc=0     # sudo must never read as "no line", mem0 Prism 4a02a919e367)
  body=$(sudo -n cat "$KEYS" 2>/dev/null) || rc=$?
  if (( rc != 0 )); then echo "UNREADABLE:$rc"; elif grep -qF "$MARK" <<<"$body"; then echo present; else echo ABSENT; fi
}
keys_has_mark() {
  local st; st=$(keys_mark_state)
  case $st in present) return 0 ;; ABSENT) return 1 ;;
    *) echo "cannot read $KEYS (sudo -n cat ${st#UNREADABLE:}): stopping" >&2; exit 3 ;; esac
}
need_sudo() { sudo -n true 2>/dev/null || { echo "non-interactive sudo is required (sudo -n true failed): nothing changed" >&2; exit 3; }; }

check() {
  echo "container=$pgc network=$net gateway=$gw"
  psql_ <<SQL
select 'role '||coalesce((select rolname||' login='||rolcanlogin||' repl='||rolreplication from pg_roles where rolname='$role'),'ABSENT');
select 'pg_monitor '||coalesce((select 'granted' from pg_auth_members m join pg_roles r on r.oid=m.member join pg_roles g on g.oid=m.roleid where r.rolname='$role' and g.rolname='pg_monitor'),'NO');
select 'max_slot_wal_keep_size '||current_setting('max_slot_wal_keep_size');
select 'hba_loaded '||count(*) from pg_hba_file_rules where '$role'=any(user_name) and 'replication'=any(database) and error is null;
select 'hba_errors '||count(*) from pg_hba_file_rules where error is not null;
select 'slot '||coalesce((select slot_name||' active='||active||' wal_status='||coalesce(wal_status,'') from pg_replication_slots where slot_name='$slot'),'ABSENT');
SQL
  local st; st=$(keys_mark_state)
  case $st in present|ABSENT) echo "key_line $st ($KEYS)" ;; *) echo "key_line UNREADABLE ($KEYS, rc=${st#UNREADABLE:})" ;; esac
}

# keys_replace: stdin becomes $KEYS. The staging file is made by mktemp (O_EXCL: it never follows an existing link,
# Prism 7ecee0da30fc) in the key file's own directory, given the live file's owner and mode, then renamed over it, so
# the live file is never truncated (Prism 41289be4a5f6). GNU stat first (BSD stat rejects -c with
# nothing on stdout; GNU `stat -f` prints a filesystem dump even when it fails).
keys_replace() {
  local tmp own mode
  tmp=$(sudo -n mktemp "$KEYS.XXXXXXXX") || { echo "cannot stage beside $KEYS" >&2; exit 3; }
  own=$(sudo -n stat -c '%u:%g' "$KEYS" 2>/dev/null || sudo -n stat -f '%u:%g' "$KEYS")
  mode=$(sudo -n stat -c '%a' "$KEYS" 2>/dev/null || sudo -n stat -f '%Lp' "$KEYS")
  if ! { sudo -n tee "$tmp" >/dev/null && sudo -n chown "$own" "$tmp" && sudo -n chmod "$mode" "$tmp" \
         && sudo -n mv -f "$tmp" "$KEYS"; }; then
    sudo -n rm -f "$tmp"; echo "writing $KEYS failed: the live file is unchanged" >&2; exit 3
  fi
}

apply() {
  : "${STANDBY_KEY_LINE:?set from gbrainctl standby init}" "${STANDBY_REPL_VERIFIER:?set from gbrainctl standby init}"
  [[ $STANDBY_REPL_VERIFIER =~ ^SCRAM-SHA-256\$[0-9]+:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+$ ]] \
    || { echo "STANDBY_REPL_VERIFIER is not a SCRAM-SHA-256 verifier" >&2; exit 2; }
  [[ $STANDBY_KEY_LINE =~ ^restrict,port-forwarding,from=\"[0-9.]+(/[0-9]{1,2})?\",permitopen=\"127\.0\.0\.1:[0-9]+\",permitopen=\"127\.0\.0\.1:[0-9]+\"\ ssh-ed25519\ [A-Za-z0-9+/=]+ ]] \
    || { echo "STANDBY_KEY_LINE is not the restricted shape init prints" >&2; exit 2; }
  # one line each: a second line would be a second, UNRESTRICTED key in authorized_keys (mem0 Prism 259e67e8b4ad)
  case $STANDBY_KEY_LINE$STANDBY_REPL_VERIFIER in *$'\n'*|*$'\r'*) echo "STANDBY_* values must be one line" >&2; exit 2 ;; esac
  need_sudo
  # create when the name is free, alter only a role that carries this setup's comment, refuse any other
  psql_ <<SQL || { echo "role step refused or failed for $role (see the error above): nothing after it ran" >&2; exit 3; }
do \$\$ declare c text; begin
  select coalesce(shobj_description(oid, 'pg_authid'), '<none>') into c from pg_roles where rolname = '$role';
  if not found then
    execute format('create role %I login replication password %L', '$role', '$STANDBY_REPL_VERIFIER');
    execute format('comment on role %I is %L', '$role', '$ROLE_MARK');
  elsif c = '$ROLE_MARK' then
    execute format('alter role %I login replication password %L', '$role', '$STANDBY_REPL_VERIFIER');
  else
    raise exception 'role % exists and is not managed by the standby setup (comment: %)', '$role', c;
  end if;
end \$\$;
SQL
  psql_ <<SQL
grant pg_monitor to $role;
alter system set max_slot_wal_keep_size = '8GB';
select pg_reload_conf();
SQL
  # the tagged line must be EXACTLY $HBA: a recreated network moves the gateway, and a stale tagged line would keep
  # refusing the new one (Prism 65fe0b3492ae). Replace the tagged line(s), keep every other line.
  edited=0
  cur=$(docker exec -e MARK="$MARK" "$pgc" sh -c 'grep -F -- "$MARK" "$PGDATA/pg_hba.conf" || true')
  if [[ $cur != "$HBA" ]]; then
    # write a same-directory sibling that carries the live file's owner and mode, then rename it over: the live
    # file is never truncated, so an interrupted run leaves it whole and a retry cannot back up a damaged copy
    # (Prism 65fe0b3492ae)
    docker exec -e MARK="$MARK" -e HBA="$HBA" "$pgc" sh -c 'set -e; f="$PGDATA/pg_hba.conf"; cp -p "$f" "$f.pre-gbrain-standby"
      cp -p "$f" "$f.new"; { grep -vF -- "$MARK" "$f" || true; printf "%s\n" "$HBA"; } > "$f.new"; mv -f "$f.new" "$f"'
    edited=1
  fi
  echo "select pg_reload_conf();" | psql_ >/dev/null
  sleep 1
  errs=$(echo "select count(*) from pg_hba_file_rules where error is not null;" | psql_)
  if [[ $errs != 0 ]]; then
    if [[ $edited == 1 ]]; then
      echo "pg_hba has $errs error line(s) after this run's edit: restoring the snapshot it took" >&2
      docker exec "$pgc" sh -c 'cp -p "$PGDATA/pg_hba.conf.pre-gbrain-standby" "$PGDATA/pg_hba.conf"'
      echo "select pg_reload_conf();" | psql_ >/dev/null
    else
      echo "pg_hba has $errs error line(s) this run did not write: left as is, fix them by hand" >&2
    fi
    exit 1
  fi
  exact=$(echo "select count(*) from pg_hba_file_rules where '$role'=any(user_name) and 'replication'=any(database) and address='$gw' and netmask='255.255.255.255' and error is null;" | psql_)
  if [[ $exact != 1 ]]; then
    echo "pg_hba: the loaded rules hold $exact replication line(s) for $role from $gw/32 (want 1)" >&2
    if [[ $edited == 1 ]]; then
      docker exec "$pgc" sh -c 'cp -p "$PGDATA/pg_hba.conf.pre-gbrain-standby" "$PGDATA/pg_hba.conf"'
      echo "select pg_reload_conf();" | psql_ >/dev/null
    fi
    exit 1
  fi
  # the tagged key line must be EXACTLY this standby's: a rebuilt standby or a rotated key replaces it, other lines
  # stay (Prism 94a4f0a892e6). A refused sudo read stops here, never reads as "no line".
  want="$STANDBY_KEY_LINE $MARK"
  body=$(sudo -n cat "$KEYS") || { echo "cannot read $KEYS (sudo -n cat): stopping" >&2; exit 3; }
  have=$(grep -F -- "$MARK" <<<"$body" || true)
  if [[ $have != "$want" ]]; then
    # same shape as pg_hba: a private sibling with the live file's owner and mode, renamed over it only after the
    # write succeeded; the live file is never truncated (Prism 41289be4a5f6)
    sudo -n cp -p "$KEYS" "$KEYS.pre-gbrain-standby"
    { [[ -z $body ]] || grep -vF -- "$MARK" <<<"$body" || true; printf '%s\n' "$want"; } | keys_replace
  fi
  check
  echo "APPLY=ok (next, on the standby: gbrainctl standby seed --host <standby>)"
}

undo() {
  need_sudo
  # the standby must be stopped first: an ACTIVE slot makes the drop fail, and the undo stops there with nothing
  # after it changed (Prism eb13f9b40a5b; the old `and not active` skipped it and reported UNDO=ok)
  psql_ <<SQL
select pg_drop_replication_slot(slot_name) from pg_replication_slots where slot_name = '$slot';
alter system reset max_slot_wal_keep_size;
SQL
  docker exec -e MARK="$MARK" "$pgc" sh -c 'set -e; f="$PGDATA/pg_hba.conf"; cp -p "$f" "$f.new"
    { grep -vF -- "$MARK" "$f" || true; } > "$f.new"; mv -f "$f.new" "$f"'
  echo "select pg_reload_conf();" | psql_ >/dev/null
  psql_ <<SQL
do \$\$ begin
  if exists (select from pg_roles where rolname = '$role' and shobj_description(oid, 'pg_authid') = '$ROLE_MARK')
  then execute format('drop role %I', '$role'); end if;
end \$\$;
SQL
  if keys_has_mark; then
    { grep -vF -- "$MARK" <<<"$(sudo -n cat "$KEYS")" || true; } | keys_replace
  fi
  check
  echo "UNDO=ok"
}

case $verb in
  check) check ;;
  apply) apply ;;
  undo) undo ;;
  *) echo "usage: $0 check|apply|undo" >&2; exit 2 ;;
esac
