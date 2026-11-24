#!/usr/bin/env python3
"""gbrainctl standby: the warm standby of the gbrain stack (card t_dd1e1edb, STANDBY.md). Nothing here promotes.

The port of mem0-selfhost-deploy lib/mem0ctl/standby.py (t_a107c291): the same verbs, link, doctor line and refusals,
minus the graph replica (gbrain is one Postgres), plus the rerank path the doctor reads.

  gbrainctl standby init   --host H         link key, pinned host key, ssh config, replication password, the docker
                                            volume on the standby dataset, an empty brain repo. Prints the NON-SECRET
                                            inputs of the primary-side step (standby/primary-enable.sh).
  gbrainctl standby seed   --host H [--reseed]
                                            pg_basebackup of the primary through the link into the standby volume
                                            (-X stream -S <slot> [-C] -R), then up primary-link + postgres (+ the
                                            rerank pair), then CREATE (never start) server and cron. Refuses a volume
                                            that already holds a cluster unless --reseed (a staged copy beside it).
  gbrainctl standby status --host H [--json]
                                            the doctor. Last line, the same shape as mem0's:
    STANDBY=FRESH|STALE lag_s=<s|unknown> ELIGIBLE=yes|no [reason=<the first failing check>] [note=...]
  gbrainctl standby repin  --host H [--no-render]
                                            after a pin bump: render, then `compose up --no-start --no-deps
                                            --force-recreate server cron` (created, never started; postgres and the
                                            link are never recreated, their id + StartedAt read before and after), then
                                            the doctor. Refused (rc 3) where server or cron runs or ROLE is primary.
  Exit 0 = FRESH and ELIGIBLE (status) / ok, 1 = anything else, 2 = usage, 3 = refused.

FRESH: the replica is in recovery and streaming with lag <= [standby] lag_page_s (60). lag_s = 0 when it has replayed
the primary's current WAL position, else the primary's pg_stat_replication.replay_lag, else the age of the last
replayed transaction (only when replay is seen NOT advancing). Primary unreachable -> lag unknown -> STALE; the replica
may still be ELIGIBLE (that is what a standby is for), and the line says when the primary was last heard from.
ELIGIBLE: not promoted, FRESH (or the primary unreachable), the CREATED server + cron carry the primary's `revision`
(image label org.opencontainers.image.revision, the label gbrainctl status reads), and neither runs.
The rerank line (proxy primary_ok from THIS host) is reported, never a refusal: the GPU host serves both boxes alike,
and the proxy's DeepInfra fallback covers it either way.

stdlib only. Secrets: the replication password and the link key live in <stack>/secrets/standby/ (0600) and reach a
child only through an env file or the container's own data directory, never argv or stdout.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import http.client
import importlib.machinery
import importlib.util
import json
import os
import re
import secrets
import subprocess
import stat
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_ctl():
    loader = importlib.machinery.SourceFileLoader("gbrainctl_lib", str(HERE / "gbrainctl"))
    spec = importlib.util.spec_from_loader("gbrainctl_lib", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


G = _load_ctl()
RenderError = G.RenderError
DEFAULT_LAG_PAGE_S = 60
PG_UID = "999:999"          # the postgres user of the pgvector (Debian) image
REQUIRED = ("primary", "link_ssh", "link_host_key", "primary_pg", "primary_serve", "repl_role", "slot",
            "application_name")


def die(msg: str, rc: int = 2):
    print(f"gbrainctl standby: {msg}", file=sys.stderr)
    sys.exit(rc)


# ---------------------------------------------------------------- config

def load(name: str | None, host_file: str | None) -> dict:
    h = G.load_host(name, host_file)
    if host_file:
        h["_path"] = host_file
    return h


def standby_cfg(host: dict) -> dict:
    sb = host.get("standby")
    if not isinstance(sb, dict):
        raise RenderError(f"{host['_file']}: no [standby] table (this host is not a standby)")
    miss = [k for k in REQUIRED if not sb.get(k)]
    if miss:
        raise RenderError(f"{host['_file']}: [standby] missing {miss}")
    for k in ("primary_pg", "primary_serve"):
        if not re.fullmatch(r"127\.0\.0\.1:\d{1,5}", str(sb[k])):
            raise RenderError(f"{host['_file']}: [standby] {k} must be the primary's loopback 127.0.0.1:<port>")
    for k in ("slot", "repl_role"):
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", sb[k]):
            raise RenderError(f"{host['_file']}: [standby] {k} must be a lower-case SQL identifier")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*@[0-9A-Za-z.:-]+", sb["link_ssh"]):
        raise RenderError(f"{host['_file']}: [standby] link_ssh must be user@host")
    out = dict(sb)
    out["lag_page_s"] = float(sb.get("lag_page_s", DEFAULT_LAG_PAGE_S))
    return out


def primary_revision(host: dict, sb: dict) -> str | None:
    """The primary's pin: hosts/<primary>.toml `revision` (or [standby] primary_file for a drill pair)."""
    p = Path(sb["primary_file"]) if sb.get("primary_file") else G.HOSTS / f"{sb['primary']}.toml"
    try:
        return str(G.load_toml(p).get("revision") or "") or None
    except RenderError:
        return None


def stack_of(host: dict) -> Path:
    return G.stack_dir(host, None)


def sdir(stack: Path) -> Path:
    return stack / "secrets" / "standby"


# ---------------------------------------------------------------- operator inputs

def scram_verifier(password: str, salt: bytes | None = None, iterations: int = 4096) -> str:
    """Postgres SCRAM-SHA-256 verifier (RFC 5802/7677), the form CREATE ROLE ... PASSWORD '<verifier>' stores as is,
    so the password itself never leaves this host."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b(salt)}${b(stored_key)}:{b(server_key)}"


def ssh_config_text(sb: dict) -> str:
    user, host = sb["link_ssh"].split("@", 1)
    return "\n".join([
        "# RENDERED by gbrainctl standby init; do not edit. The ONE path from this standby to the primary.",
        "Host primary",
        f"  HostName {host}",
        f"  Port {int(sb.get('link_port', 22))}",
        f"  User {user}",
        "  IdentityFile /run/standby/link_key",
        "  IdentitiesOnly yes",
        "  UserKnownHostsFile /run/standby/known_hosts",
        "  StrictHostKeyChecking yes",
        "  BatchMode yes",
        "  ExitOnForwardFailure yes",
        "  GatewayPorts yes",
        "  ServerAliveInterval 15",
        "  ServerAliveCountMax 3",
        f"  LocalForward 0.0.0.0:5432 127.0.0.1:{sb['primary_pg'].split(':')[1]}",
        f"  LocalForward 0.0.0.0:8199 127.0.0.1:{sb['primary_serve'].split(':')[1]}",
        ""])


def key_line(sb: dict, pub: str, src: str) -> str:
    """The line standby/primary-enable.sh adds to the primary's link keys file."""
    po = ",".join(f'permitopen="{p}"' for p in (sb["primary_pg"], sb["primary_serve"]))
    return f'restrict,port-forwarding,from="{src}",{po} {pub.strip()}'


def run(argv, **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 120)
    return subprocess.run(argv, **kw)


def cmd_init(host: dict, sb: dict, stack: Path) -> int:
    d = sdir(stack)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, stat.S_IRWXU)   # owner only: it holds the link key and the replication password
    key = d / "link_key"
    if not key.exists():
        r = run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"gbrain-standby@{host['name']}", "-f", str(key)])
        if r.returncode:
            die(f"ssh-keygen failed: {r.stderr.strip()[-200:]}")
    os.chmod(key, 0o600)
    pub = Path(str(key) + ".pub").read_text()
    link_host = sb["link_ssh"].split("@", 1)[1]
    port = int(sb.get("link_port", 22))
    kh_name = link_host if port == 22 else f"[{link_host}]:{port}"
    G.write_private(d / "known_hosts", f"{kh_name} {sb['link_host_key'].strip()}\n".encode())
    G.write_private(d / "ssh_config", ssh_config_text(sb).encode())
    pw = d / "repl_password"
    if not pw.exists():
        G.write_private(pw, secrets.token_urlsafe(32).encode())
    vf = d / "repl_verifier"     # not secret; kept so a re-run prints the SAME verifier the primary holds
    if not vf.exists():
        G.write_private(vf, scram_verifier(pw.read_text().strip()).encode())
    vol = host["pg_volume"]
    if sb.get("data_root"):
        path = f"{sb['data_root'].rstrip('/')}/pg"
        have = run(["docker", "volume", "inspect", "-f", "{{.Options.device}}", vol])
        if have.returncode == 0:
            if have.stdout.strip() != path:
                die(f"docker volume {vol} exists but binds {have.stdout.strip()!r}, not {path}")
        else:
            if not Path(sb["data_root"]).is_dir():
                die(f"{sb['data_root']} missing: the standby dataset is not mounted")
            # the dir is created by a root container (the dataset root is root-owned) and handed to postgres
            pg_image = pg_image_of(stack)
            mk = run(["docker", "run", "--rm", "-v", f"{sb['data_root']}:/r", "--entrypoint", "sh", pg_image, "-c",
                      "install -d -o 999 -g 999 -m 700 /r/pg && echo MADE"])
            if "MADE" not in mk.stdout:
                die(f"cannot create {path}: {mk.stderr.strip()[-200:]}")
            r = run(["docker", "volume", "create", "--driver", "local", "--opt", "type=none", "--opt", "o=bind",
                     "--opt", f"device={path}", vol])
            if r.returncode:
                die(f"docker volume create {vol}: {r.stderr.strip()[-200:]}")
    elif run(["docker", "volume", "inspect", vol]).returncode:
        die(f"docker volume {vol} missing and [standby] data_root unset: nothing to bind it to")
    brain = Path(os.path.expanduser(str(host.get("brain_source", ""))))
    if sb.get("data_root") and str(brain).startswith(sb["data_root"].rstrip("/") + "/") and not (brain / ".git").exists():
        # a standby host has no brain repo of its own (the exporters write the Studio's): an EMPTY repo, so a promoted
        # standby's nightly-sync finds nothing new instead of failing on a missing mount (STANDBY.md)
        uid, gid = int(host["stack_uid"]), int(host["stack_gid"])
        # the paths go in as argv, never into the shell text (Prism ce0f2932ea1f)
        mk = run(["docker", "run", "--rm", "-v", f"{sb['data_root']}:/r", "--entrypoint", "sh", pg_image_of(stack),
                  "-c", 'install -d -o "$1" -g "$2" -m 755 -- "$3" "$4" && echo MADE', "sh", str(uid), str(gid),
                  f"/r/{brain.parent.relative_to(sb['data_root'])}", f"/r/{brain.relative_to(sb['data_root'])}"])
        if "MADE" not in mk.stdout:
            die(f"cannot create {brain}: {mk.stderr.strip()[-200:]}")
        g = run(["git", "init", "-q", str(brain)])
        if g.returncode:
            die(f"git init {brain}: {g.stderr.strip()[-200:]}")
    src = sb.get("link_from") or host.get("lan_ip")
    if not src:
        die(f"{host['_file']}: lan_ip (or [standby] link_from) unset: the key line's from= needs it")
    print("# operator input for standby/primary-enable.sh on the primary (both NON-secret):")
    print(f"STANDBY_KEY_LINE='{key_line(sb, pub, src)}'")
    print(f"STANDBY_REPL_VERIFIER='{vf.read_text().strip()}'")
    print(f"STANDBY_REPL_ROLE={sb['repl_role']} STANDBY_SLOT={sb['slot']} "
          f"STANDBY_MARK='# gbrain-standby {sb.get('mark') or sb['slot']}'")
    print(f"INIT=ok host={host['name']} secrets={d}")
    return 0


PG_IMAGE_RE = re.compile(r"^\s*image:\s*(pgvector/pgvector:\S+)", re.M)


def pg_image_of(stack: Path) -> str:
    m = PG_IMAGE_RE.search((stack / "docker-compose.yaml").read_text())
    if not m:
        die("cannot read the postgres image from docker-compose.yaml")
    return m.group(1).strip("\"'")


# ---------------------------------------------------------------- docker helpers

def container(project: str, service: str) -> dict | None:
    r = run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
             "--filter", f"label=com.docker.compose.service={service}", "--format", "{{.ID}} {{.State}}"])
    rows = [ln.split() for ln in r.stdout.splitlines() if ln.strip()]
    ids = [x[0] for x in rows]
    if r.returncode or not ids:
        return None
    # a running one (a writer) wins over a newer stopped one-off with the same service label (Prism 68c4d14cd4e3)
    pick = next((x[0] for x in rows if len(x) > 1 and x[1] == "running"), ids[0])
    i = run(["docker", "inspect", pick])
    if i.returncode:
        return None
    c = json.loads(i.stdout)[0]
    return {"id": c["Id"][:12], "name": c["Name"].lstrip("/"), "status": c["State"]["Status"],
            "running": bool(c["State"]["Running"]), "image": c["Config"]["Image"],
            "revision": (c["Config"].get("Labels") or {}).get("org.opencontainers.image.revision"),
            "started_at": c["State"].get("StartedAt"), "count": len(ids)}


def env_file_with(values: dict) -> str:
    fd, p = tempfile.mkstemp(prefix=".gbrain-standby-", suffix=".env")
    with os.fdopen(fd, "w") as f:
        for k, v in values.items():
            f.write(f"{k}={v}\n")
    os.chmod(p, 0o600)
    return p


def psql(cname: str, sql: str, conninfo: str | None = None, password: str | None = None) -> tuple[int, str, str]:
    argv = ["docker", "exec"]
    envf = None
    if password is not None:
        envf = env_file_with({"PGPASSWORD": password})
        argv += ["--env-file", envf]
    argv += ["-i", cname, "sh", "-c",
             'exec psql -X -At -v ON_ERROR_STOP=1 -F "|" ' + ('"$0"' if conninfo else '-U "$POSTGRES_USER" -d postgres'),
             ] + ([conninfo] if conninfo else [])
    try:
        r = run(argv, input=sql, timeout=30)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    finally:
        if envf:
            os.unlink(envf)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def lsn(s: str | None) -> int | None:
    if not s or "/" not in s:
        return None
    hi, lo = s.split("/")
    return (int(hi, 16) << 32) + int(lo, 16)


def fnum(s) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def loopback(port: int, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
             timeout: float = 5) -> tuple[int, str]:
    """One HTTP request to 127.0.0.1:<port><path>. Loopback only: the scheme and host are fixed here, callers pass a
    port and a path (the window_lib.http_json shape; semgrep dynamic-urllib-use). Raises OSError on no answer."""
    if not path.startswith("/"):
        raise ValueError(f"path must start with /: {path!r}")
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, r.read().decode()
    finally:
        conn.close()


def http_json(port: int, path: str = "/health", timeout: float = 5) -> dict | None:
    try:
        status, text = loopback(port, "GET", path, timeout=timeout)
        return json.loads(text or "{}") if status < 400 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None


# ---------------------------------------------------------------- observe + verdict

STANDBY_SQL = """select pg_is_in_recovery(),
 coalesce((select status from pg_stat_wal_receiver), 'none'),
 coalesce((select extract(epoch from now() - last_msg_receipt_time)::text from pg_stat_wal_receiver), ''),
 coalesce(pg_last_wal_receive_lsn()::text, ''),
 coalesce(pg_last_wal_replay_lsn()::text, ''),
 coalesce(extract(epoch from now() - pg_last_xact_replay_timestamp())::text, '');"""


def primary_sql(slot: str, app: str) -> str:
    return f"""select pg_current_wal_lsn(),
 coalesce((select extract(epoch from replay_lag)::text from pg_stat_replication where application_name = '{app}' limit 1), ''),
 coalesce((select state from pg_stat_replication where application_name = '{app}' limit 1), 'absent'),
 coalesce((select wal_status from pg_replication_slots where slot_name = '{slot}'), 'absent');"""


STALL_PROBE_S = 2.0
NO_ANSWER = ("could not translate host name", "connection refused", "timeout expired", "could not connect to server",
             "no route to host", "network is unreachable", "server closed the connection unexpectedly")


def unreachable(err: str, rc: int) -> bool:
    """True only for a CONFIRMED no-answer at the CONNECT stage. A rejected login, a SQL error, or the whole-probe
    deadline (rc 124) is 'primary state unknown', never the outage exception (mem0 Prism 875f939e8910)."""
    e = err.lower()
    return rc != 124 and any(s in e for s in NO_ANSWER) and "authentication failed" not in e


def observe(host: dict, sb: dict, stack: Path) -> dict:
    proj = host["compose_project"]
    o: dict = {"host": host["name"], "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    cs = {s: container(proj, s) for s in ("primary-link", "postgres", "server", "cron", "rerank-proxy")}
    o["containers"] = {s: (c and {k: c[k] for k in ("name", "status", "running", "image", "revision", "count")})
                       for s, c in cs.items()}
    pg = cs["postgres"]
    o["pg"] = None
    o["primary"] = {"error": "not probed: replica postgres not running", "unreachable": False}
    if pg and pg["running"]:
        rc, out, err = psql(pg["name"], STANDBY_SQL)
        if rc == 0 and out:
            f = out.split("|")
            o["pg"] = {"in_recovery": f[0] == "t", "receiver": f[1], "last_msg_age_s": fnum(f[2]),
                       "receive_lsn": f[3] or None, "replay_lsn": f[4] or None, "replay_ts_age_s": fnum(f[5]),
                       "replay_advancing": None}
            rcv, rpl = lsn(f[3] or None), lsn(f[4] or None)
            if rcv is not None and rpl is not None and rcv > rpl:
                # WAL here but not replayed: measure replay PROGRESS, not a lag sample (mem0 Prism 3a64c92a2163)
                time.sleep(STALL_PROBE_S)
                rc2, out2, _ = psql(pg["name"], "select coalesce(pg_last_wal_replay_lsn()::text, '');")
                rpl2 = lsn(out2) if rc2 == 0 and out2 else None
                o["pg"]["replay_advancing"] = None if rpl2 is None else rpl2 > rpl
        else:
            o["pg"] = {"error": (err or out)[-160:]}
        pwf = sdir(stack) / "repl_password"
        o["primary"] = {"error": "not probed: no replication password (run `standby init`)", "unreachable": False}
        if pwf.exists():
            conn = (f"host=primary-link port=5432 dbname=postgres user={sb['repl_role']} "
                    f"connect_timeout=5 application_name=gbrainctl-standby-doctor")
            rc, out, err = psql(pg["name"], primary_sql(sb["slot"], sb["application_name"]), conn,
                                pwf.read_text().strip())
            if rc == 0 and out:
                f = out.split("|")
                o["primary"] = {"current_lsn": f[0], "replay_lag_s": fnum(f[1]), "sender_state": f[2],
                                "slot_wal_status": f[3]}
            else:
                e = (err or out or f"rc={rc}")
                o["primary"] = {"error": e[-160:], "unreachable": unreachable(e, rc)}
    o["pin"] = primary_revision(host, sb)
    rp = cs["rerank-proxy"]
    o["rerank"] = None
    if rp and rp["running"]:
        o["rerank"] = http_json(host['ports']['rerank'], "/health") or {"error": "no answer"}
    return o


def _r(x):
    return "unknown" if x is None else (f"{x:.1f}" if isinstance(x, float) else str(x))


def verdict(o: dict, lag_page_s: float = DEFAULT_LAG_PAGE_S) -> dict:
    """Pure: observation -> {fresh, lag_s, eligible, reason, lines, last}. Tests drive this directly."""
    lines, fails = [], []
    fail = fails.append
    cs = o.get("containers") or {}
    pg, pri = o.get("pg"), o.get("primary")
    lag = None
    primary_up = bool(pri) and "error" not in pri
    primary_down = bool(pri) and "error" in pri and bool(pri.get("unreachable"))
    if not pg:
        fail("replica postgres is not running")
        lines.append("replica   DOWN (postgres container not running)")
    elif "error" in pg:
        fail(f"replica unreadable: {pg['error']}")
        lines.append(f"replica   UNREADABLE {pg['error']}")
    elif not pg["in_recovery"]:
        fail("replica is not in recovery (promoted or never a standby)")
        lines.append("replica   NOT-IN-RECOVERY")
    else:
        rl, cl = lsn(pg.get("replay_lsn")), lsn((pri or {}).get("current_lsn")) if primary_up else None
        behind = (cl - rl) if (cl is not None and rl is not None) else None
        if primary_up and behind is not None:
            if behind <= 0:
                lag = 0.0
            else:
                rlag, age = pri.get("replay_lag_s"), pg.get("replay_ts_age_s")
                if rlag is None:
                    lag = age
                elif pg.get("replay_advancing") is False and age is not None:
                    lag = max(rlag, age)
                else:
                    lag = rlag
                lag = round(lag, 3) if lag is not None else None
        lines.append(f"replica   receiver={pg['receiver']} last_msg_age_s={_r(pg.get('last_msg_age_s'))} "
                     f"replay={pg.get('replay_lsn')} bytes_behind={behind if behind is not None else 'unknown'} "
                     f"lag_s={_r(lag)}")
        if pg["receiver"] != "streaming" and primary_up:
            fail(f"replica not streaming (receiver={pg['receiver']}) while the primary answers")
    if pri is None or ("error" in pri and not pri.get("unreachable")):
        why = (pri or {}).get("error", "not probed")
        lines.append(f"primary   UNKNOWN: {why}")
        fail(f"primary state unknown (not a confirmed outage): {why[:100]}")
    elif "error" in pri:
        lines.append(f"primary   UNREACHABLE via primary-link: {pri['error']}")
    else:
        lines.append(f"primary   reachable current={pri['current_lsn']} sender={pri['sender_state']} "
                     f"slot_wal_status={pri['slot_wal_status']}")
        if pri["slot_wal_status"] in ("lost", "absent"):
            fail(f"replication slot {pri['slot_wal_status']} on the primary: reseed")
    pin = o.get("pin")
    for svc in ("server", "cron"):
        c = cs.get(svc)
        if not c:
            fail(f"{svc} container not created (run `standby seed`)")
            lines.append(f"image     {svc} ABSENT (primary pin {(pin or '?')[:12]})")
            continue
        same = bool(pin) and c.get("revision") == pin
        lines.append(f"image     {svc} {c['image']} revision={(c.get('revision') or 'unlabelled')[:12]} "
                     f"{'==' if same else '!='} primary pin {(pin or '?')[:12]}")
        if not same:
            fail(f"{svc} revision {(c.get('revision') or 'unlabelled')[:12]} != primary pin {(pin or '?')[:12]}")
        lines.append(f"{svc:9} {'RUNNING' if c['running'] else 'stopped'} ({c['status']})")
        if c["running"]:
            fail(f"{svc} is RUNNING on the standby (exactly one cron/server may run: stop it)")
    link = cs.get("primary-link")
    lines.append(f"link      {'up' if link and link['running'] else 'DOWN'}")
    rr = o.get("rerank")
    if rr is None:
        lines.append("rerank    proxy not running (a promotion starts it)")
    elif "error" in rr:
        lines.append(f"rerank    proxy {rr['error']}")
    else:
        lines.append(f"rerank    primary_ok={rr.get('primary_ok')} (GPU host via this host's tunnel; "
                     f"false = the DeepInfra fallback would answer)")
    fresh = bool(pg) and "error" not in pg and pg.get("in_recovery") and pg.get("receiver") == "streaming" \
        and lag is not None and lag <= lag_page_s
    fresh = bool(fresh)
    if primary_up and not fresh and not fails:
        fail(f"STALE: lag_s={_r(lag)} > {lag_page_s:g}")
    eligible = not fails
    note = ""
    if primary_down and eligible:
        heard = (pg or {}).get("last_msg_age_s")
        note = (f" note=primary-unreachable last_msg_age_s={_r(heard)}" if heard is not None else
                f" note=primary-unreachable last_replay_age_s={_r((pg or {}).get('replay_ts_age_s'))}")
    reason = fails[0] if fails else ""
    last = (f"STANDBY={'FRESH' if fresh else 'STALE'} lag_s={_r(lag)} ELIGIBLE={'yes' if eligible else 'no'}"
            + (f" reason={reason}" if reason else "") + note)
    return {"fresh": fresh, "lag_s": lag, "eligible": eligible, "reason": reason, "lines": lines, "last": last,
            "primary_reachable": primary_up}


def cmd_status(host: dict, sb: dict, stack: Path, as_json: bool) -> int:
    o = observe(host, sb, stack)
    v = verdict(o, sb["lag_page_s"])
    if as_json:
        print(json.dumps({"observed": o, **{k: v[k] for k in ("fresh", "lag_s", "eligible", "reason", "last")}},
                         default=str))
    else:
        print(f"host      {host['name']} project={host['compose_project']} primary={sb['primary']} slot={sb['slot']}")
        for line in v["lines"]:
            print(line)
    print(v["last"])
    return 0 if (v["fresh"] and v["eligible"]) else 1


# ---------------------------------------------------------------- seed

def compose(host, stack, args, **kw):
    if G.rendered_for(stack / ".env") != host["name"]:
        die(f"{stack / '.env'} is not rendered for {host['name']}: run `gbrainctl render --host {host['name']}`")
    kw.setdefault("timeout", 600)
    return run(G.compose_argv(host, stack) + args, env=G.clean_env(), **kw)


NEXT, OLD = ".reseed-next", ".reseed-old"   # inside the volume: same filesystem, so the switch is renames only
MOVED = ".reseed-old-complete"              # written only once EVERY old entry is in OLD (the switch's phase 1)


def switch_script() -> str:
    """Phase 1 moves the old cluster into OLD, then writes MOVED; phase 2 moves the new copy up. restore_script()
    reads which phase was reached, so a failure in phase 1 never deletes an old file (Prism 0e1fd66f8301)."""
    return (f"set -e; cd /d; mkdir -m 700 {OLD}; "
            f"find . -mindepth 1 -maxdepth 1 ! -name {NEXT} ! -name {OLD} -exec mv -t {OLD} {{}} +; touch {MOVED}; "
            f"find {NEXT} -mindepth 1 -maxdepth 1 -exec mv -t . {{}} +; rmdir {NEXT}; echo SWITCHED")


def restore_script() -> str:
    """No OLD: nothing of the old cluster moved (mkdir failed), keep it as is. OLD without MOVED: phase 1 stopped
    part-way, no new file is up yet, so only move OLD's entries back. MOVED: the old cluster is whole in OLD, so what
    is outside it is the new copy (or part of it): remove that, then move OLD back."""
    return (f"set -e; cd /d; "
            f"if [ ! -d {OLD} ]; then rm -rf {NEXT}; echo RESTORED; exit 0; fi; "
            f"if [ -e {MOVED} ]; then find . -mindepth 1 -maxdepth 1 ! -name {OLD} ! -name {MOVED} -exec rm -rf {{}} +; fi; "
            f"find {OLD} -mindepth 1 -maxdepth 1 -exec mv -t . {{}} +; rmdir {OLD}; rm -rf {NEXT} {MOVED}; echo RESTORED")


def vsh(pg_image: str, vol: str, script: str, timeout: int = 600) -> subprocess.CompletedProcess:
    """A shell in a throwaway container with the standby volume at /d (the live postgres keeps running)."""
    return run(["docker", "run", "--rm", "-v", f"{vol}:/d", "--entrypoint", "sh", pg_image, "-c", script],
               timeout=timeout)


def basebackup(net, envf, vol, pg_image, conn, slot, create, rate, into="") -> subprocess.CompletedProcess:
    t0 = time.time()
    r = run(["docker", "run", "--rm", "--network", net, "--env-file", envf, "--user", PG_UID,
             "-v", f"{vol}:/var/lib/postgresql/data", pg_image, "pg_basebackup", "-d", conn,
             "-D", "/var/lib/postgresql/data" + (f"/{into}" if into else ""), "-X", "stream", "-S", slot, "-R",
             "-c", "spread", "-r", rate, "-v"] + (["-C"] if create else []), timeout=4 * 3600)
    print(f"basebackup rc={r.returncode} seconds={time.time() - t0:.0f} slot={slot} "
          f"{'created' if create else 'reused'}", flush=True)
    if r.returncode:
        sys.stderr.write(r.stderr[-1500:])
    return r


def pg_streaming(host) -> bool:
    c = container(host["compose_project"], "postgres")
    if not (c and c["running"]):
        return False
    rc, out, _ = psql(c["name"], "select pg_is_in_recovery()::text || '|' || "
                                 "coalesce((select status from pg_stat_wal_receiver), 'none');")
    return rc == 0 and out == "true|streaming"


def reseed_staged(host, stack, sb, vol, pg_image, net, conn, envf) -> int:
    """--reseed on a volume that holds a cluster (mem0 Prism bf207d8f26e0): the new copy is built in <vol>/.reseed-next
    on its own slot <slot>_next and verified (pg_verifybackup) while the old cluster keeps running (or stays stopped:
    the swap's old primary). Then: stop postgres, move the old cluster to .reseed-old, move the new one up, start, wait
    for it to stream. If it does not, the old cluster goes back. Only after the new copy streams is the old slot
    replaced by a copy of <slot>_next and .reseed-old removed."""
    slot, nxt, name = sb["slot"], sb["slot"] + "_next", host["name"]

    def pq(sql):
        return run(["docker", "run", "--rm", "--network", net, "--env-file", envf, pg_image, "psql", "-X", "-At",
                    "-v", "ON_ERROR_STOP=1", "-d", conn, "-c", sql])

    def slot_active(s):  # "t" / "f" / "" (absent) / None (unreadable)
        r = pq(f"select active from pg_replication_slots where slot_name = '{s}'")
        return None if r.returncode else r.stdout.strip()

    def wait_slot(s, want, tries=30):
        for _ in range(tries):
            if slot_active(s) == want:
                return True
            time.sleep(2)
        return False

    def drop_next():
        wait_slot(nxt, "f", 15)
        pq(f"select pg_drop_replication_slot(slot_name) from pg_replication_slots "
           f"where slot_name = '{nxt}' and not active")

    def abort(step, why):
        vsh(pg_image, vol, f"rm -rf /d/{NEXT}", timeout=3600)
        drop_next()
        print(f"SEED=fail host={name} step={step}: {why}; the old cluster was not touched")
        return 1

    st = vsh(pg_image, vol, f"test -e /d/{OLD} -o -e /d/{MOVED} && echo OLD; rm -rf /d/{NEXT}; "
                            f"install -d -o 999 -g 999 -m 700 /d/{NEXT} && echo STAGED", timeout=3600)
    if "OLD" in st.stdout:
        die(f"{vol}/{OLD} exists: an earlier reseed stopped mid-switch; inspect it by hand before reseeding", 3)
    if "STAGED" not in st.stdout:
        die(f"cannot stage {vol}/{NEXT}: {st.stderr.strip()[-200:]}", 1)
    drop_next()
    if basebackup(net, envf, vol, pg_image, conn, nxt, True, str(sb.get("basebackup_rate", "100M")), NEXT).returncode:
        return abort("basebackup", "base backup failed")
    vf = run(["docker", "run", "--rm", "--user", PG_UID, "-v", f"{vol}:/d", "--entrypoint", "sh", pg_image, "-c",
              f"pg_verifybackup -q /d/{NEXT} && test -f /d/{NEXT}/standby.signal && echo VERIFIED"], timeout=3600)
    print(f"verify rc={vf.returncode} {'VERIFIED' if 'VERIFIED' in vf.stdout else vf.stderr.strip()[-300:]}", flush=True)
    if "VERIFIED" not in vf.stdout:
        return abort("verify", "the new copy did not verify")

    def stopped() -> bool:
        # no file under PGDATA moves while a postmaster may hold it: the stop must succeed AND the container must
        # read not running (Prism b690e265cecb)
        r = compose(host, stack, ["stop", "postgres"], timeout=300)
        ps = run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={host['compose_project']}",
                  "--filter", "label=com.docker.compose.service=postgres", "--filter", "status=running"])
        return r.returncode == 0 and ps.returncode == 0 and not ps.stdout.strip()

    def restore(step, why):
        if not stopped():
            print(f"SEED=fail host={name} step={step}: {why}; postgres did not stop, so nothing was moved back: "
                  f"both copies are kept ({OLD} and the live dir); stop it and inspect by hand")
            return 1
        b = vsh(pg_image, vol, restore_script(), timeout=3600)
        drop_next()
        print(f"SEED=fail host={name} step={step}: {why}; "
              + ("the old cluster is back (stopped)" if "RESTORED" in b.stdout else f"RESTORE FAILED: it is in {OLD}"))
        return 1

    if not stopped():
        compose(host, stack, ["up", "-d", "--no-deps", "postgres"], timeout=600)
        return abort("stop", "postgres did not stop; nothing was moved")
    sw = vsh(pg_image, vol, switch_script(), timeout=3600)
    if "SWITCHED" not in sw.stdout:
        return restore("switch", f"moving the new copy up failed: {sw.stderr.strip()[-200:]}")
    up = compose(host, stack, ["up", "-d", "--no-deps", "postgres"], timeout=600)
    ok = up.returncode == 0 and any(pg_streaming(host) or time.sleep(2) for _ in range(90))
    if not ok:
        return restore("start", "the new copy did not come up streaming")
    print("switch ok: the new copy is streaming on the staging slot", flush=True)
    if not wait_slot(slot, "f") and slot_active(slot) != "":
        print(f"SEED=fail host={name} step=slot: {slot} is still active on the primary; "
              f"the new standby streams on {nxt}, old copy kept in {OLD}")
        return 1
    d = pq(f"select pg_drop_replication_slot(slot_name) from pg_replication_slots where slot_name = '{slot}'")
    cp = pq(f"select pg_copy_physical_replication_slot('{nxt}', '{slot}')") if d.returncode == 0 else d
    c = container(host["compose_project"], "postgres")
    rc = 1
    if cp.returncode == 0 and c:
        rc, _, _ = psql(c["name"], f"alter system set primary_slot_name = '{slot}';\nselect pg_reload_conf();")
    if rc or not wait_slot(slot, "t"):
        print(f"SEED=fail host={name} step=slot: moving the standby onto {slot} failed "
              f"({(cp.stderr or d.stderr).strip()[-200:]}); it streams on {nxt}, old copy kept in {OLD}")
        return 1
    drop_next()
    vsh(pg_image, vol, f"rm -rf /d/{OLD} /d/{MOVED}", timeout=3600)
    print(f"reseed ok: old copy removed, standby on {slot}", flush=True)
    return 0


def local_primary_problem(host: dict) -> str | None:
    """A postgres that runs here and is not a replica is a live primary: never seed over it (read F2). Unreadable
    counts as a primary. A stopped or restart-looping (guarded, exit 78) container is not running."""
    c = container(host["compose_project"], "postgres")
    if not (c and c["status"] == "running"):
        return None
    rc, out, err = psql(c["name"], "select pg_is_in_recovery();")
    if rc == 0 and out == "t":
        return None
    return f"postgres {c['name']} runs here and is not a replica (in_recovery={out if rc == 0 else 'unreadable'})"


class seed_lock:
    """One seed per stack dir: a resumed reseed must not run beside one still running (a killed coordinator leaves
    the remote seed alive; the second would delete the first's staging under its base backup; read F6)."""
    def __init__(self, stack: Path):
        self.p = stack / ".seed.lock"

    def __enter__(self):
        self.f = open(self.p, "a")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.f.close()
            die(f"another `standby seed` holds {self.p}: let it finish (or stop it) first", 3)
        return self

    def __exit__(self, *exc):
        self.f.close()


def cmd_seed(host: dict, sb: dict, stack: Path, reseed: bool) -> int:
    with seed_lock(stack):
        return seed_locked(host, sb, stack, reseed)


def seed_locked(host: dict, sb: dict, stack: Path, reseed: bool) -> int:
    pwf = sdir(stack) / "repl_password"
    if not pwf.exists():
        die("no replication password: run `gbrainctl standby init` first")
    bad = local_primary_problem(host)
    if bad:
        die(f"{bad}: refusing to seed over a live primary", 3)
    proj = host["compose_project"]
    net = f"{proj}_default"
    pg_image = pg_image_of(stack)
    r = compose(host, stack, ["up", "-d", "--no-deps", "primary-link"])
    if r.returncode:
        die(f"primary-link up failed: {r.stderr.strip()[-300:]}")
    vol = host["pg_volume"]
    # before the first mount: `docker run -v <missing name>` would CREATE a plain local volume and the base backup
    # would land on docker's disk instead of the standby dataset (Prism 2ef9e97167cb)
    have_v = run(["docker", "volume", "inspect", "-f", "{{.Options.device}}", vol])
    if have_v.returncode:
        die(f"docker volume {vol} missing: run `gbrainctl standby init` first", 3)
    if sb.get("data_root") and have_v.stdout.strip() != f"{sb['data_root'].rstrip('/')}/pg":
        die(f"docker volume {vol} binds {have_v.stdout.strip()!r}, not {sb['data_root'].rstrip('/')}/pg", 3)
    probe = vsh(pg_image, vol, "test -s /d/PG_VERSION && echo HAS || echo EMPTY")
    has = "HAS" in probe.stdout
    if has and not reseed:
        die(f"volume {vol} already holds a cluster; refusing (pass --reseed to build a new copy beside it)", 3)
    for _ in range(30):
        if run(["docker", "run", "--rm", "--network", net, pg_image, "pg_isready", "-h", "primary-link", "-p",
                "5432"]).returncode == 0:
            break
        time.sleep(2)
    else:
        die("the primary is not reachable through primary-link (pg_isready): is the primary step applied?", 4)
    conn = (f"host=primary-link port=5432 user={sb['repl_role']} dbname=postgres "
            f"application_name={sb['application_name']}")
    envf = env_file_with({"PGPASSWORD": pwf.read_text().strip()})
    try:
        have = run(["docker", "run", "--rm", "--network", net, "--env-file", envf, pg_image, "psql", "-X", "-At",
                    "-d", conn, "-c", f"select count(*) from pg_replication_slots where slot_name = '{sb['slot']}'"])
        if have.returncode:
            die(f"cannot query the primary as {sb['repl_role']}: {have.stderr.strip()[-200:]}", 4)
        if has:
            # the copy replaces a cluster: its source must be a promoted primary, not another replica (read F2)
            src = run(["docker", "run", "--rm", "--network", net, "--env-file", envf, pg_image, "psql", "-X", "-At",
                       "-d", conn, "-c", "select pg_is_in_recovery()"])
            if src.returncode or src.stdout.strip() != "f":
                die(f"--reseed: the source through primary-link is not a primary (in_recovery="
                    f"{src.stdout.strip() if src.returncode == 0 else 'unreadable'}): refusing to replace {vol}", 3)
            if reseed_staged(host, stack, sb, vol, pg_image, net, conn, envf):
                return 1
        else:
            vsh(pg_image, vol, "chown 999:999 /d && chmod 700 /d")
            rate = str(sb.get("basebackup_rate", "100M"))
            if basebackup(net, envf, vol, pg_image, conn, sb["slot"], have.stdout.strip() == "0", rate).returncode:
                return 1
    finally:
        os.unlink(envf)
    r = compose(host, stack, ["up", "-d", "--no-deps", "postgres"])
    if r.returncode:
        die(f"compose up postgres failed: {r.stderr.strip()[-300:]}", 1)
    # SEED=ok means a replica that streams, not a started container (Prism a6df8203dcb4)
    if not any(pg_streaming(host) or time.sleep(2) for _ in range(90)):
        print(f"SEED=fail host={host['name']} step=stream: postgres is up but not in recovery + streaming after 180s")
        return 1
    # the rerank pair runs on a standby too (stateless; the doctor proves the GPU path from here)
    r = compose(host, stack, ["up", "-d", "--no-deps", "rerank-proxy"]
                + (["rerank-tunnel"] if host.get("rerank_tunnel") else []))
    print(f"up rerank rc={r.returncode}" + (f" {r.stderr.strip()[-200:]}" if r.returncode else ""))
    r = compose(host, stack, ["--profile", "promoted", "create", "--no-recreate", "server", "cron"])
    print(f"create server cron rc={r.returncode}" + (f" {r.stderr.strip()[-200:]}" if r.returncode else ""))
    if r.returncode:
        print(f"SEED=fail host={host['name']} step=create (the replica is up; server/cron containers are not)")
        return 1
    print(f"SEED=ok host={host['name']} slot={sb['slot']}")
    return 0


# ---------------------------------------------------------------- repin

def cmd_repin(host: dict, sb: dict, stack: Path, no_render: bool) -> int:
    name = host["name"]
    if G.read_role(stack) == "primary":
        print(f"REPIN=refused host={name} reason=ROLE is primary: a primary is recreated with `gbrainctl up`")
        return 3
    cs = {s: container(host["compose_project"], s) for s in ("server", "cron", "postgres", "primary-link")}
    live = [s for s in ("server", "cron") if cs[s] and cs[s]["running"]]
    if live:
        print(f"REPIN=refused host={name} reason={','.join(live)} running here: this host is serving")
        return 3
    pin = primary_revision(host, sb)
    if pin != host.get("revision"):
        print(f"REPIN=refused host={name} reason=this host's revision {str(host.get('revision'))[:12]} != primary pin "
              f"{(pin or '?')[:12]}: re-pin hosts/{name}.toml first")
        return 3
    for img in (host["server_image"], host["cron_image"]):
        if run(["docker", "image", "inspect", img]).returncode:
            print(f"REPIN=refused host={name} reason=image {img} not local (gbrainctl build --host {name})")
            return 3
    if not no_render:
        a = argparse.Namespace(host=None, host_file=host.get("_path"), stack_dir=None)
        if not a.host_file:
            a.host = name
        if G.cmd_render(a):
            print(f"REPIN=fail host={name} step=render")
            return 1
    before = {s: (c and (c["id"], c["started_at"])) for s, c in cs.items() if s in ("postgres", "primary-link")}
    r = compose(host, stack, ["--profile", "promoted", "up", "--no-start", "--no-deps", "--force-recreate",
                              "server", "cron"])
    if r.returncode:
        print(f"REPIN=fail host={name} step=recreate {r.stderr.strip()[-300:]}")
        return 1
    after = {s: (c and (c["id"], c["started_at"])) for s in before
             for c in [container(host["compose_project"], s)]}
    moved = [s for s in before if before[s] != after[s]]
    print(f"untouched: " + " ".join(f"{s}={(after[s] or ('-',))[0]}" for s in after) + (f" MOVED={moved}" if moved else ""))
    v = verdict(observe(host, sb, stack), sb["lag_page_s"])
    print(v["last"])
    ok = not moved and v["eligible"]
    print(f"REPIN={'ok' if ok else 'fail'} host={name} revision={host['revision'][:12]}")
    return 0 if ok else 1


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gbrainctl standby")
    ap.add_argument("verb", choices=["init", "seed", "status", "repin"])
    ap.add_argument("--host")
    ap.add_argument("--host-file")
    ap.add_argument("--reseed", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-render", action="store_true")
    a = ap.parse_args(argv)
    try:
        host = load(a.host, a.host_file)
        sb = standby_cfg(host)
        stack = stack_of(host)
    except RenderError as e:
        die(str(e))
    if a.verb == "init":
        return cmd_init(host, sb, stack)
    if a.verb == "seed":
        return cmd_seed(host, sb, stack, a.reseed)
    if a.verb == "repin":
        return cmd_repin(host, sb, stack, a.no_render)
    return cmd_status(host, sb, stack, a.json)


if __name__ == "__main__":
    sys.exit(main())
