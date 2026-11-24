#!/usr/bin/env python3
"""gbrainctl swap: make the standby the gbrain primary, in either direction (card t_dd1e1edb, STANDBY.md).

The port of mem0-selfhost-deploy lib/mem0ctl/swap.py (t_91373a1c / t_390439a0): the same journal, lock, FENCE record,
host-side verbs over ssh, refusals and resume/undo rules. What differs for gbrain, and why:
  - no graph replica: fence and catchup prove the Postgres checkpoint only.
  - no DNS flip: every gbrain caller is on the Studio and calls 127.0.0.1:8199 (the mem0 prefetch, rail.sh, the MCP
    shim). The flip is the OLD primary's own primary-link, which publishes that loopback port and forwards it to the
    new primary's server (docker-compose.standby.yaml). Swap back: the Studio's own server publishes it again.
  - no outage swap (`--old-primary-is-down` is refused): the callers live on the old primary, so promoting the NAS
    while the Studio is down serves nobody, and the Studio coming back would restart its own postgres beside it.
    Losing the Studio = the replica keeps the data; swap when the Studio answers again (or reseed it from the NAS).
  - prove: a real `search` over MCP on the callers' host loopback, as the rail-cli OAuth client (the clients live
    in the replicated database, so the same credentials work against either primary).

  gbrainctl swap --to <host> [--no-reseed] [--dry-run]
  gbrainctl swap --to-file <profile> ...          (drill pairs: standby/drill/)
  gbrainctl swap resume <id> | undo <id> | status [<id>]

<host> is the STANDBY to promote (hosts/<host>.toml with a [standby] table naming the primary it follows). Run it
from any box that reaches both; each host's own steps run ON that host as `gbrainctl swap _<verb>` (locally, or over
its `[swap] ssh` in its stack_dir). Steps, each journaled:
  1 preflight   target doctor ELIGIBLE; the old primary answers and is not in recovery; the old host's [standby]
                follows the target (else --no-reseed); the target's [link] keys_file is set
  2 fence       old: FENCE records this swap's id (another swap's live fence is refused), ROLE=standby, server + cron
                stopped, ARMED cleared (was_armed recorded), postgres stopped; proven: pg_controldata "shut down"
  3 catchup     target: replay LSN >= that checkpoint (zero loss)
  4 promote     the old primary re-fenced (same id: its CURRENT shutdown checkpoint), catchup re-proven against it,
                still fenced by THIS swap; target: pg_promote(), primary_conninfo + slot
                reset, ROLE=primary, link removed, postgres recreated without the standby overlay; not in recovery
  5 start       target: ARMED as the old one was, server + cron + rerank up, /health engine=postgres, exactly 1 cron
                running there and 0 on the old primary
  6 flip        the target accepts the old host's link key + replication role (standby/primary-enable.sh apply),
                then the old host's primary-link starts: its loopback serve port now answers with the new primary
  7 prove       on the callers' host: /health engine=postgres and a real MCP `search` returning hits, through the
                loopback port the callers use
  8 reseed_old  the old host re-seeds as the new standby (`standby seed --reseed`), doctor polled until it follows
Undo (old primary back) only before promote_started is journaled (the promotion may still finish on its own after
that, even when its answer was lost); from there only resume. Last line
SWAP=ok|failed|refused|undone|dry-run id=<id> ... Exit 0 ok, 1 step failed, 2 usage, 3 refused.
"""
from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gbrain_standby as SB  # noqa: E402

G = SB.G
RenderError = G.RenderError
STEPS = ["preflight", "fence", "catchup", "promote", "start", "flip", "prove", "reseed_old"]
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15"]
NO_ANSWER = ("connection timed out", "connection refused", "no route to host", "network is unreachable",
             "could not resolve hostname", "operation timed out", "host is down")
PG_DATA = "/var/lib/postgresql/data"
CATCHUP_S = 120
FENCE_FILE = "FENCE"
SID_RE = re.compile(r"[0-9TZ]+-[a-z0-9-]+")
HEALTH_WAIT_S = 300
RESEED_SETTLE_S = 300
STOP_GRACE_S = 30
PROVE_QUERY = "gbrain standby swap"
SCRAM_RE = re.compile(r"SCRAM-SHA-256\$[^\s'\"|,;)]+")


def scrub(text: str) -> str:
    """No SCRAM verifier in a journal, a SIDE= error or a step result (psql's CONTEXT line quotes the statement)."""
    return SCRAM_RE.sub("SCRAM-SHA-256$<redacted>", text)


class Refused(Exception):
    pass


class StepFail(Exception):
    pass


class Unreachable(Exception):
    pass


class FenceHeld(StepFail):
    """The host is fenced by another swap: this coordinator is refused, and the verb changed nothing."""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def is_drill(host: dict) -> bool:
    return bool((host.get("swap") or {}).get("drill"))


def run(argv, *, timeout=300, check=False, **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    r = subprocess.run(argv, timeout=timeout, **kw)
    if check and r.returncode:
        raise StepFail(f"{' '.join(map(str, argv[:4]))} rc={r.returncode}: {(r.stderr or '').strip()[-300:]}")
    return r


def wait(fn, seconds: float, every: float = 2.0) -> bool:
    t0 = time.time()
    while True:
        if fn():
            return True
        if time.time() - t0 >= seconds:
            return False
        time.sleep(every)


def health(port: int) -> dict | None:
    return SB.http_json(port, "/health", timeout=5)


def health_ok(port: int) -> bool:
    h = health(port)
    return bool(h and h.get("status") == "ok" and h.get("engine") == "postgres")


# ====================================================================== host-local verbs (`swap _<verb>`)

class Local:
    def __init__(self, host: dict):
        self.host = host
        self.stack = G.stack_dir(host, None)
        self.project = host["compose_project"]

    def compose(self, *args, timeout=600, check=True):
        if G.rendered_for(self.stack / ".env") != self.host["name"]:
            raise StepFail(f"{self.stack / '.env'} is not rendered for {self.host['name']}")
        return run(G.compose_argv(self.host, self.stack) + list(args), env=G.clean_env(), timeout=timeout,
                   check=check)

    def ctr(self, svc: str) -> dict | None:
        r = run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={self.project}",
                 "--filter", f"label=com.docker.compose.service={svc}", "--format", "{{.Names}}|{{.State}}|{{.Image}}"])
        if r.returncode:   # docker unreadable is not "no container": a fence check must never read it as stopped
            raise StepFail(f"docker ps rc={r.returncode}: {(r.stderr or '').strip()[-200:]}")
        rows = [ln.split("|") for ln in r.stdout.split()]
        if not rows:
            return None
        n, st, img = rows[0]
        return {"name": n, "running": st == "running", "state": st, "image": img, "count": len(rows),
                "running_count": sum(1 for x in rows if x[1] == "running")}

    def running(self, svc: str) -> bool:
        c = self.ctr(svc)
        return bool(c and c["running"])

    def psql(self, sql: str) -> tuple[int, str, str]:
        c = self.ctr("postgres")
        if not c or not c["running"]:
            return 1, "", "postgres not running"
        r = run(["docker", "exec", "-i", c["name"], "sh", "-c",
                 'exec psql -X -At -v ON_ERROR_STOP=1 -F "|" -U "$POSTGRES_USER" -d postgres'], input=sql, timeout=60)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def role(self) -> str | None:
        return G.read_role(self.stack)

    def write_role(self, role: str | None) -> None:
        f = self.stack / G.ROLE_FILE
        if role is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(role + "\n")

    def pages(self) -> int | None:
        c = self.ctr("postgres")
        if not c or not c["running"]:
            return None
        r = run(["docker", "exec", "-i", c["name"], "sh", "-c",
                 'exec psql -X -At -U "$POSTGRES_USER" -d gbrain -c "select count(*) from pages"'], timeout=60)
        return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


def armed(L: Local, op: str) -> bool | None:
    """/state/ARMED in this host's cron-state volume (the cron ticks only where it exists, so exactly one host may hold
    it). op: test | set | clear. A one-shot root container on the cron container's volumes, so it works with the cron
    stopped. None = no cron container."""
    c = L.ctr("cron")
    if not c:
        return None
    sh = {"test": "test -f /state/ARMED && echo yes || echo no",
          "set": "touch /state/ARMED && test -f /state/ARMED && echo yes",
          "clear": "rm -f /state/ARMED && test ! -e /state/ARMED && echo no"}[op]
    r = run(["docker", "run", "--rm", "--user", "0:0", "--volumes-from", c["name"], "--entrypoint", "sh",
             c["image"], "-c", sh], timeout=120, check=True)
    return r.stdout.strip().endswith("yes")


def read_fence(L: Local) -> dict | None:
    try:
        rec = json.loads((L.stack / FENCE_FILE).read_text())
    except FileNotFoundError:
        return None
    except ValueError:
        rec = None
    return rec if isinstance(rec, dict) and rec.get("swap_id") else {"swap_id": "?unreadable", "at": None}


def write_fence(L: Local, rec: dict) -> None:
    tmp = L.stack / (FENCE_FILE + ".tmp")
    tmp.write_text(json.dumps(rec, sort_keys=True) + "\n")
    os.replace(tmp, L.stack / FENCE_FILE)


class host_lock:
    def __init__(self, L: Local):
        self.p = L.stack / ".fence.lock"

    def __enter__(self):
        self.f = open(self.p, "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.f.close()


def need_sid(a) -> str:
    sid = getattr(a, "swap_id", None)
    if not sid or not SID_RE.fullmatch(sid):
        raise StepFail(f"--swap-id <the swap's id> is required (a fence belongs to one swap), got {sid!r}")
    return sid


def v_status(L: Local, _a) -> dict:
    cs = {s: L.ctr(s) for s in ("postgres", "server", "cron", "primary-link", "rerank-proxy")}
    rc, out, err = L.psql("select pg_is_in_recovery(), case when pg_is_in_recovery() then "
                          "coalesce(pg_last_wal_replay_lsn()::text,'') else pg_current_wal_lsn()::text end;")
    pg = {"in_recovery": out.split("|")[0] == "t", "lsn": out.split("|")[1]} if rc == 0 and "|" in out else \
        {"error": (err or "unreadable")[-160:]}
    fence = read_fence(L)
    return {"host": L.host["name"], "role_file": L.role(),
            "fence": fence and {"swap_id": fence.get("swap_id"), "at": fence.get("at")},
            "containers": {s: (c and {"running": c["running"], "state": c["state"], "n": c["running_count"]})
                           for s, c in cs.items()},
            "pg": pg, "pages": L.pages(), "health": health(L.host["ports"]["serve"])}


def v_doctor(L: Local, _a) -> dict:
    sb = SB.standby_cfg(L.host)
    v = SB.verdict(SB.observe(L.host, sb, L.stack), sb["lag_page_s"])
    return {k: v[k] for k in ("eligible", "fresh", "lag_s", "reason", "last", "primary_reachable")}


def pg_controldata(name: str) -> dict:
    img = run(["docker", "inspect", "-f", "{{.Config.Image}}", name], check=True).stdout.strip()
    r = run(["docker", "run", "--rm", "--user", SB.PG_UID, "--volumes-from", name, "--entrypoint", "pg_controldata",
             img, PG_DATA], timeout=120, check=True)
    d = dict(ln.split(":", 1) for ln in r.stdout.splitlines() if ":" in ln)
    return {"state": d.get("Database cluster state", "").strip(),
            "checkpoint_lsn": d.get("Latest checkpoint location", "").strip(),
            "timeline": d.get("Latest checkpoint's TimeLineID", "").strip()}


def v_fence(L: Local, a) -> dict:
    """Stop the writers, then Postgres, and prove it. FENCE first (this swap owns the fence), then ROLE=standby: from
    here a `compose up` on this host composes the standby set, whose server and cron never start."""
    sid = need_sid(a)
    with host_lock(L):
        rec = read_fence(L)
        if rec and rec.get("swap_id") != sid:
            if L.role() == "standby":
                raise FenceHeld(f"{L.host['name']} is fenced by swap {rec.get('swap_id')} (since {rec.get('at')}), not "
                                f"{sid}: one coordinator per fence; finish or undo swap {rec.get('swap_id')} first")
            rec = None
        if rec is None:
            rec = {"swap_id": sid, "at": now(), "role_before": L.role() or "none", "was_armed": None}
            write_fence(L, rec)
        L.write_role("standby")
    out = {"swap_id": sid, "role_before": rec["role_before"]}
    live = [c["name"] for c in (L.ctr("server"), L.ctr("cron")) if c and c["running"]]
    if live:   # the callers' error window starts here
        run(["docker", "stop", "-t", str(STOP_GRACE_S), *live], timeout=STOP_GRACE_S + 60, check=True)
    out["server_running"], out["cron_running"] = L.running("server"), L.running("cron")
    if out["server_running"] or out["cron_running"]:
        raise StepFail(f"fence: server running={out['server_running']} cron running={out['cron_running']} after stop")
    if is_drill(L.host) and os.environ.get("GBRAIN_SWAP_INJECT_FAIL") == "fence-after-stop":
        raise StepFail("fence: injected failure after the writers stopped (GBRAIN_SWAP_INJECT_FAIL, drill only)")
    if rec.get("was_armed") is None:
        rec["was_armed"] = bool(armed(L, "test"))
        write_fence(L, rec)
    out["was_armed"] = rec["was_armed"]
    if armed(L, "clear") is not False and L.ctr("cron"):
        raise StepFail("fence: /state/ARMED still present after clear")
    pgc = L.ctr("postgres")
    if not pgc:
        raise StepFail("fence: no postgres container on the old primary")
    if pgc["running"]:
        rc, cur, _ = L.psql("select pg_current_wal_lsn();")
        out["last_lsn_before_stop"] = cur if rc == 0 else None
        run(["docker", "stop", "-t", "120", pgc["name"]], timeout=180, check=True)
    out["pg_running"] = L.running("postgres")
    if out["pg_running"]:
        raise StepFail("fence: postgres still running after docker stop")
    cd = pg_controldata(pgc["name"])
    out.update({"pg_state": cd["state"], "checkpoint_lsn": cd["checkpoint_lsn"], "timeline": cd["timeline"]})
    if cd["state"] != "shut down":
        raise StepFail(f"fence: pg_controldata says {cd['state']!r}, not 'shut down' (not a clean stop)")
    # The stopped container still carries the primary's entrypoint: `compose restart postgres` (or `docker start`)
    # would bring the old cluster back writable. Recreate it, NOT started, from the standby composition (ROLE=standby
    # is written above), whose entrypoint refuses to start without standby.signal (Prism 9caefce1c941).
    if not pg_guarded(pgc["name"]):
        L.compose("up", "--no-start", "--no-deps", "--force-recreate", "postgres", timeout=600)
        pgc = L.ctr("postgres")
        if not (pgc and not pgc["running"] and pg_guarded(pgc["name"])):
            raise StepFail("fence: the stopped postgres container is not the standby-guarded one after recreate")
    out["pg_guarded"] = True
    return out


def pg_guarded(name: str) -> bool:
    """The container's command is the standby overlay's guard (exit 78 without standby.signal)."""
    r = run(["docker", "inspect", "-f", "{{json .Config.Cmd}}", name])
    return r.returncode == 0 and "standby.signal" in r.stdout and "exit 78" in r.stdout


def v_unfence(L: Local, a) -> dict:
    """undo of fence: only THIS swap's fence, back to the role it had, postgres + server + cron up; proven: not in
    recovery, /health engine=postgres. Idempotent; no record = this swap changed nothing here."""
    sid = need_sid(a)
    with host_lock(L):
        rec = read_fence(L)
        if rec is None:
            return {"skipped": f"no fence recorded on {L.host['name']}: swap {sid} changed nothing here",
                    "role": L.role()}
        if rec.get("swap_id") != sid:
            raise FenceHeld(f"{L.host['name']} is fenced by swap {rec.get('swap_id')}, not {sid}: refusing to "
                            f"unfence another swap's fence")
    prev = rec.get("role_before")
    L.write_role(prev if prev in ("primary", "standby") else None)
    link = L.ctr("primary-link")
    if link and prev != "standby":   # a flip that ran before the failure: the old primary's own server needs the port
        run(["docker", "rm", "-f", link["name"]], timeout=60, check=True)
    L.compose("up", "-d", "--no-deps", "postgres", timeout=600)
    got = {"v": None}

    def primary_now():
        rc, out, _ = L.psql("select pg_is_in_recovery();")
        got["v"] = out if rc == 0 else None
        return got["v"] == "f"

    if not wait(primary_now, 120):
        raise StepFail(f"unfence: postgres in_recovery={got['v']!r} (want f)")
    if rec.get("was_armed"):
        armed(L, "set")
    L.compose("up", "-d", timeout=900)
    ok = wait(lambda: health_ok(L.host["ports"]["serve"]), HEALTH_WAIT_S, 3)
    if not ok:
        raise StepFail(f"unfence: /health did not say engine=postgres within {HEALTH_WAIT_S}s")
    (L.stack / FENCE_FILE).unlink(missing_ok=True)
    return {"pg_in_recovery": False, "health_ok": ok, "cron_running": L.running("cron"), "role": L.role()}


def lsn(s: str | None) -> int | None:
    return SB.lsn(s)


def v_catchup(L: Local, a) -> dict:
    want = lsn(a.checkpoint_lsn)
    got = {"lsn": None}

    def pg_done():
        rc, out, _ = L.psql("select coalesce(pg_last_wal_replay_lsn()::text, '');")
        got["lsn"] = out if rc == 0 else None
        v = lsn(got["lsn"])
        return v is not None and want is not None and v >= want

    t0 = time.time()
    if not wait(pg_done, a.timeout):
        raise StepFail(f"catchup: replay {got['lsn']} < old primary's final checkpoint {a.checkpoint_lsn} "
                       f"after {a.timeout}s")
    return {"replay_lsn": got["lsn"], "checkpoint_lsn": a.checkpoint_lsn, "pg_s": round(time.time() - t0, 1)}


CLEAR_CONNINFO_SQL = ("alter system reset primary_conninfo;\nalter system reset primary_slot_name;\n"
                      "select pg_reload_conf();")
STALE_CONNINFO_SQL = "select count(*) from pg_file_settings where name in ('primary_conninfo', 'primary_slot_name');"


def v_promote(L: Local, _a) -> dict:
    rc, rec, err = L.psql("select pg_is_in_recovery();")
    if rc:
        raise StepFail(f"promote: postgres unreadable: {err[-200:]}")
    out = {"was_in_recovery": rec == "t"}
    if rec == "t":
        rc, ok, err = L.psql("select pg_promote(true, 60);")
        if rc or ok != "t":
            raise StepFail(f"promote: pg_promote -> {ok!r} {err[-200:]}")
    # pg_basebackup -R left primary_conninfo (with the replication password) in postgresql.auto.conf: a promoted
    # host keeps no password of a role it no longer uses (mem0 t_7a36b3f0). Idempotent for a resume.
    rc, _, err = L.psql(CLEAR_CONNINFO_SQL)
    rc2, left, _ = L.psql(STALE_CONNINFO_SQL)
    if rc or rc2 or left != "0":
        raise StepFail(f"promote: primary_conninfo/primary_slot_name not reset ({left or err[-200:]} left)")
    L.write_role("primary")
    (L.stack / FENCE_FILE).unlink(missing_ok=True)
    link = L.ctr("primary-link")
    if link:   # it publishes the serve port this host's own server needs from now on
        run(["docker", "rm", "-f", link["name"]], timeout=60, check=True)
    L.compose("up", "-d", "--no-deps", "postgres", timeout=600)
    got = {"v": None}

    def primary_now():
        rc, o, _ = L.psql("select pg_is_in_recovery();")
        got["v"] = o if rc == 0 else None
        return got["v"] == "f"

    if not wait(primary_now, 120):
        raise StepFail(f"promote: postgres in_recovery={got['v']!r} after recreate")
    rc, tl, _ = L.psql("select timeline_id from pg_control_checkpoint();")
    out.update({"pg_in_recovery": False, "timeline": tl, "link_removed": bool(link), "pages": L.pages()})
    return out


def v_start(L: Local, a) -> dict:
    L.compose("create", "cron", timeout=600)
    arm = None if is_drill(L.host) else armed(L, "set" if a.arm == "yes" else "clear")
    L.compose("up", "-d", timeout=900)   # ROLE=primary: the full set, server + cron + rerank
    ok = wait(lambda: health_ok(L.host["ports"]["serve"]), HEALTH_WAIT_S, 3)
    c = L.ctr("cron")
    out = {"health_ok": ok, "cron_count": c["running_count"] if c else 0,
           "armed": arm if arm is not None else "drill: never armed"}
    if not ok:
        raise StepFail(f"start: /health did not say engine=postgres within {HEALTH_WAIT_S}s")
    if out["cron_count"] != 1:
        raise StepFail(f"start: {out['cron_count']} cron running on the new primary (want exactly 1)")
    return out


def v_inputs(L: Local, _a) -> dict:
    """The non-secret inputs a primary needs to accept this host as its standby (what `standby init` prints)."""
    sb = SB.standby_cfg(L.host)
    d = SB.sdir(L.stack)
    if not (d / "link_key.pub").exists() or not (d / "repl_verifier").exists():
        r = run([sys.executable, str(HERE / "gbrainctl"), "standby", "init", *profile_args(L.host)], timeout=300)
        if r.returncode:
            raise StepFail(f"standby init failed: {r.stderr.strip()[-200:]}")
    src = sb.get("link_from") or L.host.get("lan_ip")
    return {"key_line": SB.key_line(sb, (d / "link_key.pub").read_text(), src),
            "verifier": (d / "repl_verifier").read_text().strip(), "role": sb["repl_role"], "slot": sb["slot"],
            "mark": str(sb.get("mark") or sb["slot"])}


def v_primary_enable(L: Local, a) -> dict:
    keys = (L.host.get("link") or {}).get("keys_file")
    if not keys:
        raise StepFail(f"{L.host['_file']}: [link] keys_file unset: where this host's link sshd reads its keys")
    # the verifier arrives on stdin, never argv (ps shows argv on both hosts, ssh's remote command too)
    verifier = sys.stdin.readline().strip()
    if not verifier.startswith("SCRAM-SHA-256$"):
        raise StepFail("primary-enable: no SCRAM verifier on stdin")
    pgc = L.ctr("postgres")
    env = dict(os.environ, STANDBY_KEY_LINE=a.key_line, STANDBY_REPL_VERIFIER=verifier, STANDBY_REPL_ROLE=a.role,
               STANDBY_SLOT=a.slot, STANDBY_MARK=f"# gbrain-standby {a.mark or a.slot}",
               STANDBY_KEYS=os.path.expanduser(keys), GBRAIN_PG_CONTAINER=pgc["name"] if pgc else "")
    r = run(["bash", str(HERE / "standby" / "primary-enable.sh"), "apply"], env=env, timeout=300)
    tail = [scrub(ln) for ln in (r.stdout + r.stderr).strip().splitlines()[-10:]]
    if r.returncode or "APPLY=ok" not in r.stdout:
        raise StepFail("primary-enable apply failed: " + " | ".join(tail)[-400:])
    return {"applied": True, "tail": tail}


def v_link_up(L: Local, _a) -> dict:
    """The flip on the OLD primary: its primary-link (standby overlay, ROLE=standby since the fence) starts alone, and
    its loopback serve port must answer as the new primary's server."""
    if L.role() != "standby":
        raise StepFail(f"link_up: ROLE={L.role()!r} on {L.host['name']} (want standby: only a fenced host links)")
    if L.running("server"):
        raise StepFail("link_up: this host's own server runs: it would hold the serve port")
    L.compose("up", "-d", "--no-deps", "primary-link", timeout=300)
    port = L.host["ports"]["serve"]
    ok = wait(lambda: health_ok(port), 90, 2)
    if not ok:
        raise StepFail(f"link_up: 127.0.0.1:{port}/health did not answer engine=postgres through the link in 90s")
    return {"serve_port": port, "health": health(port)}


def v_prove(L: Local, a) -> dict:
    """A real search, the way the callers make it: /health, an OAuth client_credentials token from the client env file
    the profile names, then MCP tools/call search on this host's loopback serve port."""
    port = L.host["ports"]["serve"]
    h = health(port)
    if not (h and h.get("engine") == "postgres"):
        raise StepFail(f"prove: 127.0.0.1:{port}/health = {h}")
    envp = os.path.expanduser(a.client_env or (L.host.get("swap") or {}).get("prove_client_env") or "")
    if not envp or not Path(envp).exists():
        raise StepFail(f"prove: no client env file ({envp or '[swap] prove_client_env unset'})")
    env = {}
    for line in Path(envp).read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep:
            env[k.strip()] = v.strip().strip("'\"")
    form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": env.get("GBRAIN_RAIL_CLIENT_ID", ""),
                                   "client_secret": env.get("GBRAIN_RAIL_CLIENT_SECRET", "")}).encode()
    try:
        status, text = SB.loopback(port, "POST", "/token", form,
                                   {"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
        if status >= 400:
            raise StepFail(f"prove: token: HTTP {status}")
        tok = json.loads(text).get("access_token")
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise StepFail(f"prove: token: {type(e).__name__}")
    if not tok:
        raise StepFail("prove: token endpoint returned no access_token")
    q = a.query or PROVE_QUERY
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "search", "arguments": {"query": q, "limit": 3}}}).encode()
    hdr = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
           "Accept": "application/json, text/event-stream"}
    t0 = time.time()
    try:
        status, raw = SB.loopback(port, "POST", "/mcp", body, hdr, timeout=90)
    except (OSError, http.client.HTTPException) as e:
        raise StepFail(f"prove: search: {type(e).__name__}: {e}")
    if status >= 400:
        raise StepFail(f"prove: search: HTTP {status}")
    ms = round((time.time() - t0) * 1000)
    data = "".join(ln[5:].lstrip() for ln in raw.splitlines() if ln.startswith("data:")) or raw
    try:
        d = json.loads(data)
    except ValueError:
        raise StepFail(f"prove: search answer is not JSON: {raw[:120]!r}")
    res = d.get("result") or {}
    text = ((res.get("content") or [{}])[0] or {}).get("text", "")
    if "error" in d or res.get("isError") or not text.strip():
        raise StepFail(f"prove: search error/empty: {json.dumps(d)[:200]}")
    try:
        hits = json.loads(text)
        n = len(hits if isinstance(hits, list) else hits.get("results", []))
    except ValueError:
        n = len([ln for ln in text.splitlines() if ln.strip()])
    if n < 1:
        raise StepFail(f"prove: search {q!r} returned no hits")
    return {"health": h, "query": q, "hits": n, "search_ms": ms, "client": Path(envp).name}


def reseed_problems(L: Local, sid: str) -> list[str]:
    """Why this host's cluster may NOT be discarded for swap <sid>: it must still be that swap's fenced old primary
    (FENCE names the swap, ROLE=standby, no server or cron) and its postgres must not run as a primary. A host put back
    by hand, or an old command from scrollback, reads as one of these."""
    p = []
    rec = read_fence(L)
    if not rec or rec.get("swap_id") != sid:
        p.append(f"FENCE names swap {rec.get('swap_id') if rec else None}, not {sid}")
    if L.role() != "standby":
        p.append(f"ROLE={L.role()!r} (want standby)")
    live = [s for s in ("server", "cron") if L.running(s)]
    if live:
        p.append("runs " + ",".join(live))
    c = L.ctr("postgres")
    if c and c["state"] == "running":
        rc, out, _ = L.psql("select pg_is_in_recovery();")
        if not (rc == 0 and out == "t"):
            p.append(f"postgres runs and is not a replica (in_recovery={out if rc == 0 else 'unreadable'})")
    return p


def v_reseed(L: Local, a) -> dict:
    sid = need_sid(a)
    with host_lock(L):
        bad = reseed_problems(L, sid)
    if bad:
        raise StepFail(f"reseed refused on {L.host['name']}: {'; '.join(bad)}: it is not the fenced old primary of "
                       f"swap {sid}, so its cluster is not discarded (STANDBY.md \"Promoted but not serving\")")
    t0 = time.time()
    r = run([sys.executable, str(HERE / "gbrainctl"), "standby", "seed", "--reseed", *profile_args(L.host)],
            timeout=4 * 3600)
    tail = (r.stdout + r.stderr).strip().splitlines()[-8:]
    if r.returncode or not any(ln.startswith("SEED=ok") for ln in tail):
        raise StepFail("standby seed --reseed failed: " + " | ".join(tail)[-500:])
    with host_lock(L):   # past the point of undo only now: the host streams as the new standby (SEED=ok)
        rec = read_fence(L)
        if rec and rec.get("swap_id") == sid:
            (L.stack / FENCE_FILE).unlink(missing_ok=True)
    return {"seconds": round(time.time() - t0), "tail": tail}


VERBS = {"_status": v_status, "_doctor": v_doctor, "_fence": v_fence, "_unfence": v_unfence, "_catchup": v_catchup,
         "_promote": v_promote, "_start": v_start, "_inputs": v_inputs, "_primary_enable": v_primary_enable,
         "_link_up": v_link_up, "_prove": v_prove, "_reseed": v_reseed}


def profile_args(host: dict) -> list[str]:
    f = host.get("_path")
    return ["--host-file", f] if f else ["--host", host["name"]]


def side_main(verb: str, argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog=f"gbrainctl swap {verb}")
    ap.add_argument("--host")
    ap.add_argument("--host-file")
    for k in ("checkpoint-lsn", "key-line", "role", "slot", "mark", "client-env", "query"):
        ap.add_argument(f"--{k}")
    ap.add_argument("--swap-id")
    ap.add_argument("--arm", choices=("yes", "no"), default="no")
    ap.add_argument("--timeout", type=int, default=CATCHUP_S)
    a = ap.parse_args(argv)
    try:
        host = SB.load(a.host, a.host_file)
        res = VERBS[verb](Local(host), a)
    except FenceHeld as e:
        print("SIDE=" + json.dumps({"ok": False, "refused": True, "error": scrub(str(e))[-600:]}))
        return 3
    except (StepFail, RenderError, subprocess.SubprocessError, OSError, KeyError) as e:
        print("SIDE=" + json.dumps({"ok": False, "error": scrub(f"{type(e).__name__}: {e}")[-600:]}))
        return 1
    print("SIDE=" + json.dumps({"ok": True, **res}, default=str))
    return 0


# ====================================================================== the orchestrator

def this_box(ip: str) -> bool:
    import socket
    if not ip or ip.startswith("127."):
        return True
    if os.environ.get("GBRAIN_SWAP_FORCE_REMOTE") == "1":
        return False
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.connect((ip, 9))
        own = u.getsockname()[0]
        u.close()
        return own == ip
    except OSError:
        return False


class Side:
    """One host as the orchestrator sees it: its verbs run locally or over `[swap] ssh` in its own stack_dir."""

    def __init__(self, host: dict):
        self.host, self.name = host, host["name"]
        sw = host.get("swap") or {}
        self.remote = sw.get("ssh") if sw.get("ssh") and not this_box(host.get("lan_ip", "")) else None
        self.python = sw.get("python") or "python3"

    def call(self, verb: str, *args: str, timeout: int = 900, stdin: str | None = None) -> dict:
        cmd = [verb, *profile_args(self.host), *args]
        if not self.remote:
            argv = [sys.executable, str(HERE / "gbrainctl"), "swap", *cmd]
        else:
            sd = self.host["stack_dir"]
            sd = sd.replace("~", "$HOME", 1) if sd.startswith("~") else shlex.quote(sd)
            remote = f"cd {sd} && {shlex.quote(self.python)} ./gbrainctl swap " + " ".join(shlex.quote(c) for c in cmd)
            argv = ["ssh", *SSH_OPTS, self.remote, "bash -c " + shlex.quote(remote)]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=stdin or "")
        except subprocess.TimeoutExpired:
            raise StepFail(f"{self.name} {verb}: no answer in {timeout}s")
        if self.remote and r.returncode == 255 and any(s in r.stderr.lower() for s in NO_ANSWER):
            raise Unreachable(f"{self.name}: ssh {self.remote}: {r.stderr.strip()[-200:]}")
        line = next((ln for ln in reversed(r.stdout.splitlines()) if ln.startswith("SIDE=")), None)
        if not line:
            raise StepFail(scrub(f"{self.name} {verb} rc={r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}"))
        res = json.loads(line[5:])
        if not res.get("ok"):
            if res.get("refused"):
                raise Refused(f"{self.name} {verb}: {res.get('error')}")
            raise StepFail(f"{self.name} {verb}: {res.get('error')}")
        return res


def state_root() -> Path:
    return Path(os.path.expanduser(os.environ.get("GBRAIN_SWAP_STATE", "~/.local/state/gbrain-swap")))


def following(doc: dict) -> bool:
    return bool(doc.get("eligible") and doc.get("fresh") and doc.get("primary_reachable"))


def fence_problems(os_: dict, sid: str) -> list[str]:
    cs = os_.get("containers") or {}
    p = []
    live = [s for s in ("server", "cron", "postgres") if (cs.get(s) or {}).get("running")]
    if live:
        p.append("runs " + ",".join(live))
    if os_.get("role_file") != "standby":
        p.append(f"ROLE={os_.get('role_file')!r}")
    fid = (os_.get("fence") or {}).get("swap_id")
    if fid != sid:
        p.append(f"fenced by swap {fid}" if fid else "no fence record")
    return p


class Swap:
    def __init__(self, sid: str, st: dict, target: Side, old: Side, callers: Side, root: Path):
        self.id, self.st, self.target, self.old, self.callers, self.root = sid, st, target, old, callers, root
        self.dir = root / sid

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / ".state.json.tmp"
        tmp.write_text(json.dumps(self.st, indent=1, sort_keys=True, default=str) + "\n")
        os.replace(tmp, self.dir / "state.json")

    def log(self, event: str, **kw):
        rec = {"at": now(), "t": round(time.time(), 3), "event": event, **kw}
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "journal.jsonl", "a") as f:
            f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
        print(f"  {rec['at']} {event} " + " ".join(f"{k}={v}" for k, v in kw.items() if k != "result")[:400],
              flush=True)

    def done(self, step: str) -> bool:
        return step in self.st["steps"]

    def s_preflight(self):
        try:
            return self._preflight()
        except StepFail as e:
            raise Refused(str(e))

    def _preflight(self):
        t, o, st = self.target, self.old, self.st
        if is_drill(t.host) != is_drill(o.host):
            raise Refused(f"{t.name} and {o.name}: one is a drill profile and one is not")
        if not (t.host.get("link") or {}).get("keys_file"):
            raise Refused(f"{t.host['_file']}: [link] keys_file unset: the old primary could not follow {t.name}")
        doc = t.call("_doctor")
        st["doctor"] = doc
        self.log("doctor", host=t.name, last=doc["last"])
        if not doc["eligible"]:
            raise Refused(f"{t.name} doctor: ELIGIBLE=no ({doc['reason']})")
        try:
            os_ = o.call("_status", timeout=120)
        except Unreachable as e:
            raise Refused(f"{o.name} does not answer ({e}). gbrain has no outage swap: its callers live on "
                          f"{o.name}; the replica on {t.name} keeps the data (STANDBY.md)")
        st["old_status"] = os_
        if os_.get("pg", {}).get("in_recovery"):
            raise Refused(f"{o.name} is itself a replica (in recovery): it is not the primary {t.name} follows")
        if not doc.get("primary_reachable"):
            raise Refused(f"{t.name}'s doctor does not reach {o.name} through the link, but {o.name} answers ssh")
        bad = self.reseed_source_problem()
        if bad and not st.get("no_reseed"):
            raise Refused(f"automatic reseed would follow the wrong primary: {bad} (fix it, or --no-reseed)")
        if not isinstance(o.host.get("standby"), dict):
            raise Refused(f"{o.host['_file']} has no [standby] table: after the swap its callers could not reach "
                          f"{t.name} (the flip is {o.name}'s primary-link)")
        return {"doctor": doc["last"], "old_pages": os_.get("pages"), "old_health": os_.get("health"),
                "callers_host": self.callers.name}

    def reseed_source_problem(self) -> str | None:
        sb = self.old.host.get("standby")
        if not isinstance(sb, dict):
            return f"{self.old.host['_file']} has no [standby] table"
        if sb.get("primary") != self.target.name:
            return f"{self.old.host.get('_file')} [standby] primary = {sb.get('primary')!r}, not {self.target.name!r}"
        return None

    def fence_cmd(self) -> str:
        return f"./gbrainctl swap _fence {' '.join(profile_args(self.old.host))} --swap-id {self.id}"

    def recheck_fenced(self, step: str) -> dict:
        try:
            os_ = self.old.call("_status", timeout=60)
        except Unreachable as e:
            raise StepFail(f"{step}: cannot read {self.old.name} to prove it is still fenced by {self.id} ({e})")
        bad = fence_problems(os_, self.id)
        if bad:
            raise StepFail(f"{step}: {self.old.name} is not fenced by this swap ({'; '.join(bad)}): refusing to "
                           f"promote beside it; re-fence it (`{self.fence_cmd()}`) then resume, or undo")
        return {"old_fenced_by": self.id}

    def s_fence(self):
        r = self.old.call("_fence", "--swap-id", self.id, timeout=600)
        self.st["fence"] = r
        return r

    def s_catchup(self):
        f = self.st.get("fence") or {}
        if not f.get("checkpoint_lsn"):
            raise StepFail("catchup: the fence recorded no checkpoint LSN: nothing to prove against")
        return self.target.call("_catchup", "--checkpoint-lsn", f["checkpoint_lsn"], "--timeout", str(CATCHUP_S),
                                timeout=CATCHUP_S * 2 + 60)

    def s_promote(self):
        # Re-read the old primary's shutdown checkpoint now (the fence verb is idempotent for this swap id) and prove
        # catchup against THAT checkpoint: a re-fence after a restart writes a newer one, and the catchup journaled
        # before it does not cover the WAL in between (Prism 0f16f7498863).
        f = self.old.call("_fence", "--swap-id", self.id, timeout=600)
        prev = (self.st.get("fence") or {}).get("checkpoint_lsn")
        if f.get("checkpoint_lsn") != prev:
            self.log("refenced", checkpoint_was=prev, checkpoint_now=f.get("checkpoint_lsn"))
            self.st["fence"] = {**(self.st.get("fence") or {}), **f}
            self.save()
        try:
            pg = self.target.call("_status", timeout=120).get("pg") or {}
        except Unreachable as e:
            raise StepFail(f"promote: cannot read {self.target.name} ({e})")
        if "in_recovery" not in pg:
            raise StepFail(f"promote: {self.target.name} postgres unreadable ({pg.get('error')})")
        if not pg["in_recovery"]:
            # Resume after _promote ran but its result was not journaled (Prism 3cd6acbcced2): there is no replay
            # position left to prove, and the catchup was proven before promote_started was written. Without that
            # marker a primary target means something else promoted it: refuse.
            ps = self.st.get("promote_started")
            if not ps:
                raise StepFail(f"promote: {self.target.name} is already a primary and this swap never started "
                               f"its promotion: refusing")
            # the old primary must not have moved past what the target proved it replayed: a newer fence checkpoint
            # means it wrote after that proof, and those writes are not on the promoted target (Prism ef0d99a7f1ee)
            proven = ps.get("checkpoint_lsn") if isinstance(ps, dict) else None
            if not proven or proven != f.get("checkpoint_lsn"):
                raise StepFail(f"promote: {self.target.name} was promoted after proving checkpoint {proven}, but "
                               f"{self.old.name}'s fence checkpoint is now {f.get('checkpoint_lsn')}: it may hold "
                               f"writes the new primary lacks; resolve by hand, no automatic resume")
            chk = self.recheck_fenced("promote")
            self.log("promote_resumed", promote_started=ps)
            return {**self.target.call("_promote", timeout=900), **chk, "resumed_after_promote": True,
                    "checkpoint_lsn": f.get("checkpoint_lsn")}
        cu = self.s_catchup()
        chk = self.recheck_fenced("promote")
        self.st["promote_started"] = {"at": now(), "checkpoint_lsn": f.get("checkpoint_lsn"),
                                      "replay_lsn": cu.get("replay_lsn")}
        self.save()
        return {**self.target.call("_promote", timeout=900), **chk, "catchup_replay_lsn": cu.get("replay_lsn"),
                "checkpoint_lsn": f.get("checkpoint_lsn")}

    def s_start(self):
        arm = "yes" if (self.st.get("fence") or {}).get("was_armed") else "no"
        r = self.target.call("_start", "--arm", arm, timeout=1200)
        os_ = self.old.call("_status", timeout=120)
        n = ((os_.get("containers") or {}).get("cron") or {}).get("n", 0)
        r["old_cron_running"] = n
        if n:
            raise StepFail(f"{self.old.name} runs {n} cron after the fence: two crons")
        return r

    def s_flip(self):
        inp = self.old.call("_inputs", timeout=300)
        pe = self.target.call("_primary_enable", "--key-line", inp["key_line"], "--role", inp["role"],
                              "--slot", inp["slot"], "--mark", inp.get("mark") or inp["slot"],
                              stdin=inp["verifier"] + "\n", timeout=600)
        lk = self.old.call("_link_up", timeout=300)
        return {"primary_enable": pe["tail"][-2:], "old_link": lk}

    def s_prove(self):
        return self.callers.call("_prove", *(["--client-env", self.st["client_env"]] if self.st.get("client_env")
                                            else []), timeout=300)

    def s_reseed_old(self):
        o = self.old
        if self.st.get("no_reseed"):
            cmds = [f"# on {o.name}: ./gbrainctl swap _reseed {' '.join(profile_args(o.host))} --swap-id {self.id}"]
            print("  reseed_old not run (--no-reseed); the command:")
            for c in cmds:
                print("    " + c)
            return {"skipped": "--no-reseed", "commands": cmds}
        try:
            doc = o.call("_doctor", timeout=300)
        except StepFail:
            doc = {"eligible": False}
        rs = {"seconds": 0, "skipped": "already a FRESH standby of this primary (a resume)"}
        if not following(doc):
            rs = o.call("_reseed", "--swap-id", self.id, timeout=4 * 3600 + 600)
        t0 = time.time()
        while True:
            doc = o.call("_doctor", timeout=300)
            if following(doc) or time.time() - t0 > RESEED_SETTLE_S:
                break
            time.sleep(10)
        if not following(doc):
            raise StepFail(f"{o.name} reseeded but after {RESEED_SETTLE_S}s its doctor says {doc['last']}")
        return {"reseed_s": rs["seconds"], "doctor": doc["last"], "settle_s": round(time.time() - t0, 1),
                **({"note": rs["skipped"]} if "skipped" in rs else {})}

    def run_steps(self) -> int:
        for step in STEPS:
            if self.done(step):
                continue
            self.st["at_step"] = step
            self.save()
            self.log("step_start", step=step)
            t0 = time.time()
            try:
                if is_drill(self.target.host) and os.environ.get("GBRAIN_SWAP_INJECT_FAIL") == step:
                    raise StepFail(f"injected failure at {step} (GBRAIN_SWAP_INJECT_FAIL, drill only)")
                res = getattr(self, f"s_{step}")()
            except Refused as e:
                self.st["state"] = "refused"
                self.st["error"] = str(e)
                self.save()
                self.log("refused", step=step, reason=str(e))
                print(f"SWAP=refused id={self.id} step={step} reason={e}")
                return 3
            except (StepFail, Unreachable, RenderError) as e:
                err = scrub(str(e))
                self.st["state"] = "failed"
                self.st["error"] = err
                self.save()
                self.log("step_failed", step=step, error=err)
                print(f"SWAP=failed id={self.id} step={step} reason={err}")
                print(f"  next: {self.next_hint(step)}")
                return 1
            self.st["steps"][step] = {"seconds": round(time.time() - t0, 1), "at": now(), "result": res}
            self.save()
            self.log("step_done", step=step, seconds=round(time.time() - t0, 1), result=res)
        self.st["state"] = "ok"
        self.save()
        tot = sum(v["seconds"] for v in self.st["steps"].values())
        print(f"SWAP=ok id={self.id} to={self.target.name} from={self.old.name} steps={len(STEPS)}/{len(STEPS)} "
              f"seconds={tot:.0f}")
        return 0

    def next_hint(self, step: str) -> str:
        if step == "preflight" or (step == "fence" and not self.st.get("fence")):
            return (f"fix the cause, then `gbrainctl swap resume {self.id}`; or `gbrainctl swap undo {self.id}` "
                    f"(the fence may have stopped part of {self.old.name})")
        if not self.done("promote") and not self.st.get("promote_started"):
            return (f"`gbrainctl swap resume {self.id}` (continues), or `gbrainctl swap undo {self.id}` (restarts "
                    f"{self.old.name} as the primary; allowed until the promotion of {self.target.name} starts)")
        return (f"`gbrainctl swap resume {self.id}` only: the promotion of {self.target.name} started, so undo could "
                f"make two primaries or lose writes; back = a second swap once {self.old.name} is a standby again "
                f"(STANDBY.md \"Promoted but not serving\" when resume cannot finish)")

    def undo(self) -> int:
        ps = self.st.get("promote_started")
        if ps or self.done("promote"):
            # the promotion may still be running on the target (a killed coordinator leaves the remote _promote
            # alive; pg_promote(true, 60) can answer f and still finish): one in_recovery read cannot prove it will not
            # become a primary after the old one is unfenced (read F1)
            at = ps.get("at") if isinstance(ps, dict) else None
            print(f"SWAP=refused id={self.id} reason=the promotion of {self.target.name} started ({at}): undo could "
                  f"leave two primaries; `gbrainctl swap resume {self.id}` is the only way on")
            return 3
        try:
            ts = self.target.call("_status", timeout=120)
        except (StepFail, Unreachable) as e:
            print(f"SWAP=refused id={self.id} reason=cannot read {self.target.name}: {e}")
            return 3
        if not ts.get("pg", {}).get("in_recovery"):
            print(f"SWAP=refused id={self.id} reason={self.target.name} is no longer a replica (promoted): undo "
                  f"would discard what it took; resume instead")
            return 3
        before = self.st.get("state")
        self.st["state"] = "undoing"
        self.save()
        self.log("undo_start")
        try:
            r = self.old.call("_unfence", "--swap-id", self.id, timeout=1200)
        except Refused as e:
            self.st["state"] = before
            self.save()
            self.log("undo_refused", reason=str(e))
            print(f"SWAP=refused id={self.id} step=undo reason={e}")
            return 3
        except (StepFail, Unreachable) as e:
            self.st["error"] = str(e)
            self.save()
            self.log("undo_failed", error=str(e))
            print(f"SWAP=failed id={self.id} step=undo reason={e} (state undoing: only `undo` may continue)")
            return 1
        self.st["state"] = "undone"
        self.st["undo"] = r
        self.save()
        self.log("undo_done", result=r)
        print(f"SWAP=undone id={self.id} primary={self.old.name}")
        return 0


def load_side(name: str | None, path: str | None) -> Side:
    try:
        return Side(SB.load(None if path else name, path))
    except RenderError as e:
        raise Refused(str(e))


def load_trio(to: str | None, to_file: str | None) -> tuple[Side, Side, Side]:
    t = load_side(to, to_file)
    sb = t.host.get("standby")
    if not isinstance(sb, dict) or not sb.get("primary"):
        raise Refused(f"{t.host['_file']}: no [standby] table: {t.name} is not a standby, nothing to promote")
    o = load_side(sb["primary"], sb.get("primary_file"))
    cname = (t.host.get("swap") or {}).get("callers_host") or o.name
    by = {t.name: t, o.name: o}
    if cname in by:
        c = by[cname]
    else:
        c = load_side(cname, (t.host.get("swap") or {}).get("callers_file"))
    return t, o, c


def identity(side: Side) -> dict:
    h = side.host
    return {"name": h["name"], "lan_ip": h.get("lan_ip"), "project": h.get("compose_project"),
            "stack_dir": h.get("stack_dir"), "ssh": (h.get("swap") or {}).get("ssh"), "path": h.get("_path")}


class lock:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.p = root / ".lock"

    def __enter__(self):
        self.f = open(self.p, "a")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.f.close()
            raise Refused(f"another gbrainctl swap command holds {self.p}")
        return self

    def __exit__(self, *exc):
        self.f.close()


def open_swaps(root: Path) -> list[str]:
    out = []
    for d in sorted(root.glob("*/state.json")) if root.exists() else []:
        try:
            st = json.loads(d.read_text())
        except ValueError:
            continue
        if st.get("state") not in ("ok", "undone", "refused", "dry-run"):
            out.append(d.parent.name)
    return out


def load_swap(sid: str, root: Path) -> Swap:
    p = root / sid / "state.json"
    if not SID_RE.fullmatch(sid) or not p.exists():
        raise Refused(f"no swap {sid} under {root}")
    st = json.loads(p.read_text())
    t, o, c = load_trio(st["target"]["name"] if not st["target"].get("path") else None, st["target"].get("path"))
    for side, key in ((t, "target"), (o, "old")):
        if identity(side) != st[key]:
            raise Refused(f"swap {sid}: the {key} profile now names another stack ({identity(side)} != {st[key]})")
    return Swap(sid, st, t, o, c, root)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in VERBS:
        return side_main(argv[0], argv[1:])
    ap = argparse.ArgumentParser(prog="gbrainctl swap")
    ap.add_argument("cmd", nargs="?", choices=["resume", "undo", "status"])
    ap.add_argument("sid", nargs="?")
    ap.add_argument("--to")
    ap.add_argument("--to-file")
    ap.add_argument("--no-reseed", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--client-env", help="the OAuth client env file prove uses (default: the callers host's)")
    ap.add_argument("--old-primary-is-down", help="refused: gbrain has no outage swap (STANDBY.md)")
    a = ap.parse_args(argv)
    root = state_root()
    try:
        if a.old_primary_is_down is not None:
            raise Refused("gbrain has no outage swap: its callers live on the old primary; the replica keeps the data "
                          "until it answers again (STANDBY.md \"Outage\")")
        if a.cmd == "status":
            ids = [a.sid] if a.sid else sorted(p.parent.name for p in root.glob("*/state.json"))[-5:]
            for sid in ids:
                st = json.loads((root / sid / "state.json").read_text())
                print(f"{sid} state={st.get('state')} at_step={st.get('at_step')} "
                      f"to={st['target']['name']} from={st['old']['name']} steps={list(st.get('steps', {}))}")
            print(f"SWAP=status open={','.join(open_swaps(root)) or '-'}")
            return 0
        if a.cmd in ("resume", "undo"):
            if not a.sid:
                raise Refused(f"usage: gbrainctl swap {a.cmd} <id>")
            with lock(root):
                sw = load_swap(a.sid, root)
                if a.cmd == "undo":
                    return sw.undo()
                if sw.st.get("state") == "undoing":
                    raise Refused(f"swap {a.sid} is being undone: only `gbrainctl swap undo {a.sid}` may continue")
                if sw.st.get("state") == "ok":
                    print(f"SWAP=ok id={a.sid} (already finished)")
                    return 0
                sw.st["state"] = "running"
                sw.log("resume")
                return sw.run_steps()
        if not (a.to or a.to_file):
            raise Refused("usage: gbrainctl swap --to <standby host> [--no-reseed] [--dry-run]")
        with lock(root):
            t, o, c = load_trio(a.to, a.to_file)
            pend = open_swaps(root)
            if pend and not a.dry_run:
                raise Refused(f"unfinished swap(s) {','.join(pend)}: resume or undo first")
            sid = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-to-{t.name}"
            st = {"id": sid, "state": "running", "steps": {}, "target": identity(t), "old": identity(o),
                  "callers": c.name, "no_reseed": a.no_reseed, "client_env": a.client_env, "started": now()}
            sw = Swap(sid, st, t, o, c, root)
            if a.dry_run:
                sw.dir = root / ".dry-run" / sid
                res = sw.s_preflight()
                print(f"SWAP=dry-run id={sid} " + json.dumps(res, default=str))
                return 0
            sw.save()
            sw.log("start", to=t.name, frm=o.name, callers=c.name)
            return sw.run_steps()
    except Refused as e:
        print(f"SWAP=refused reason={e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
