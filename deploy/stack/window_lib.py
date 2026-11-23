"""window_lib.py: the step runner shared by gbrain-cutover and gbrain-rollback (card t_bb0e9194). stdlib only.

A window is a list of Steps. Each step has the commands it runs and a readback that says whether it took.
  dry run (default)  prints every step: its commands and its readback, and runs ONLY the readbacks and the read-only
                     preflight probes (docker inspect, launchctl print, hermes cron list, ...). Nothing is changed.
  --apply            runs each step's commands, then its readback; a readback that is not ok STOPS the window with
                     the step id and what it read. A step whose readback is already ok before it runs is skipped
                     (`already`). A cutover that stopped after C1 is undone with gbrain-rollback (each rollback
                     step checks its own state first, so it is safe from any point).
  --drill            the same window on a scratch copy (a scratch host profile: another project, pg container,
                     volume and ports). Steps that touch host-only production state (launchd jobs, hermes cron rows,
                     the brain repo's git config, rail.sh, the backup stamp, ARMED) print `SKIP (drill)` and run
                     nothing. The docker / gbrainctl steps, which are the downtime, run for real against the copy.
Every line is prefixed with the seconds since the window started; the last lines are RESULT= and DOWNTIME_S=.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent

# The production host's launchd-era names (read on the Studio 2026-11-23; README "Cutover"). Not host-profile keys:
# they exist only on the one host being cut over, and the drill skips every step that uses them.
LAUNCHD_LABELS = ("ai.gbrain.serve", "ai.gbrain.rerank-proxy", "ai.gbrain.rerank-tunnel")
CRON_NIGHTLY_SYNC = "9b1a4fdaaa2a"      # gbrain-nightly-sync (export + import): paused by the cutover, kept as is
CRON_PG_BACKUP = "a3bcde3fa92e"         # gbrain-pg-backup
CRON_DEPLOY_PARITY = "7d0b9686f64e"     # gbrain-deploy-parity
CRON_OBSERVER_EXPORT = "2127ecdb32ee"   # gbrain-observer-export (hermes-home PR, ships paused): resumed by cutover
CRON_STACK_WATCH = "70c54de14efe"       # gbrain-stack-watch (hermes-home PR, ships paused): resumed by cutover
LAUNCHD_ERA_SUFFIX = "-launchd-era"


class Stop(Exception):
    pass


@dataclass
class Step:
    sid: str
    title: str
    cmds: list[list[str]] = field(default_factory=list)   # run in order under --apply; a non-zero rc stops
    readback: Callable[[], tuple[bool, str]] | None = None
    expect: str = ""
    prod_only: bool = False      # host-only production state: skipped under --drill
    run: Callable[[], None] | None = None   # a step whose action is not a plain argv list (waits, polls)
    downtime_start: bool = False
    downtime_end: bool = False
    readonly: bool = False       # a preflight probe: runs in dry run too, and its readback must be ok
    show: list[str] = field(default_factory=list)   # printed like cmds, run by `run` (conditional actions)
    always: bool = False         # run even when the readback is already ok (a check whose old result could pass)
    dry_readback: bool = True    # False: the readback itself writes state (stack-watch's episode file): apply only


class Window:
    def __init__(self, name: str, apply: bool, drill: bool):
        self.name, self.apply, self.drill = name, apply, drill
        self.t0 = time.monotonic()
        self.down_t: float | None = None
        self.up_t: float | None = None

    def say(self, msg: str = ""):
        for line in (msg.splitlines() or [""]):
            print(f"[{time.monotonic() - self.t0:7.1f}s] {line}", flush=True)

    def run_steps(self, steps: list[Step]) -> int:
        mode = "APPLY" if self.apply else "DRY-RUN (changes nothing)"
        self.say(f"{self.name}: {mode}{' DRILL' if self.drill else ''}")
        self.preflight_fail: list[str] = []
        try:
            for s in steps:
                self.one(s)
        except Stop as e:
            self.say(f"STOPPED: {e}")
            self.result("stopped")
            return 1
        if self.preflight_fail:      # dry run only: an apply stops at the first one
            self.result(f"preflight-fail({','.join(self.preflight_fail)})")
            return 1
        self.result("ok")
        return 0

    def result(self, what: str):
        self.say(f"RESULT={what} window={self.name} mode={'apply' if self.apply else 'dry-run'}"
                 f"{' drill' if self.drill else ''}")
        if self.down_t is not None and self.up_t is not None:
            self.say(f"DOWNTIME_S={self.up_t - self.down_t:.1f}")

    def one(self, s: Step):
        self.say(f"== {s.sid} {s.title}")
        for c in s.cmds:
            self.say(f"   $ {shlex.join(c)}")
        for c in s.show:
            self.say(f"   $ {c}")
        if s.prod_only and self.drill:
            self.say("   SKIP (drill: production host state)")
            return
        if s.expect:
            self.say(f"   readback expects: {s.expect}")
        if s.readonly:
            ok, text = s.readback() if s.readback else (True, "")
            self.say(f"   readback: {'ok' if ok else 'PREFLIGHT FAIL'}: {text}")
            if not ok:
                if self.apply:
                    raise Stop(f"{s.sid} preflight not ok, nothing was changed by this step: {text}")
                self.preflight_fail.append(s.sid)
            return
        if not self.apply:
            if s.readback and not s.dry_readback:
                self.say("   readback runs only under --apply (it records state)")
            elif s.readback:
                ok, text = s.readback()
                self.say(f"   readback now (before the step): {'ok' if ok else 'not yet'}: {text}")
            return
        if s.downtime_start:
            self.down_t = time.monotonic()
            self.say("   DOWNTIME STARTS")
        if s.readback and not s.always:
            ok, text = s.readback()
            if ok:
                self.say(f"   already: {text}")
                return
        for c in s.cmds:
            r = subprocess.run(c, capture_output=True, text=True)
            out = (r.stdout + r.stderr).strip()
            if out:
                self.say("   | " + out[-1500:].replace("\n", "\n   | "))
            if r.returncode != 0:
                raise Stop(f"{s.sid} `{shlex.join(c)}` rc={r.returncode}")
        if s.run:
            s.run()
        if s.downtime_end:
            self.up_t = time.monotonic()
            self.say("   DOWNTIME ENDS")
        if s.readback:
            ok, text = s.readback()
            self.say(f"   readback: {text}")
            if not ok:
                raise Stop(f"{s.sid} readback not ok: {text}")


# ---- probes (read-only) ----------------------------------------------------------------------------------------
def sh(*cmd: str, timeout: float = 60, input: str | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, input=input)
        return r.returncode, (r.stdout.strip() or r.stderr.strip())
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def load_host(host: str | None, host_file: str | None, stack_dir: str | None) -> dict:
    p = Path(host_file) if host_file else HERE / "hosts" / f"{host}.toml"
    h = tomllib.loads(p.read_text())
    h["_stack"] = Path(os.path.expanduser(stack_dir or h["stack_dir"]))
    h["_hostargs"] = (["--host-file", str(p)] if host_file else ["--host", str(host)]) + \
        (["--stack-dir", str(stack_dir)] if stack_dir else [])
    return h


def drill_clashes(h: dict) -> list[str]:
    """A --drill profile's docker targets that a production profile (hosts/*.toml with compose_project "gbrain")
    also names: pg container, data volume, published ports. C4/C5 act on pg_container outside compose, so the
    project-name guard alone let a scratch profile stop the live database (Prism e8731ada0c50)."""
    out = []
    for p in sorted((HERE / "hosts").glob("*.toml")):
        prod = tomllib.loads(p.read_text())
        if prod.get("compose_project") != "gbrain":
            continue
        for k in ("pg_container", "pg_volume"):
            if h.get(k) and h.get(k) == prod.get(k):
                out.append(f"{k}={h[k]} is {p.name}'s")
        prod_ports = set((prod.get("ports") or {}).values())
        for k, v in sorted((h.get("ports") or {}).items()):
            if v in prod_ports:
                out.append(f"ports.{k}={v} is a {p.name} port")
    return out


def gbrainctl(h: dict, *args: str) -> list[str]:
    """argv for this checkout's gbrainctl; the host args go before a `--` (what follows it goes to compose)."""
    a = list(args)
    i = a.index("--") if "--" in a else len(a)
    return [sys.executable, str(HERE / "gbrainctl"), *a[:i], *h["_hostargs"], *a[i:]]


def docker_inspect(name: str, fmt: str) -> str | None:
    rc, out = sh("docker", "inspect", "-f", fmt, name)
    return out if rc == 0 else None


def project_containers(project: str) -> list[str]:
    rc, out = sh("docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
                 "--format", "{{.Names}}")
    return out.split() if rc == 0 else []


def http_json(port: int, path: str = "/health", timeout: float = 3) -> dict | None:
    """GET http://127.0.0.1:<port><path> as JSON. Loopback only: the scheme and
    host are fixed here, callers pass only a port and a path."""
    import http.client
    if not path.startswith("/"):
        return None
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        if r.status >= 400:
            return None
        return json.loads(r.read().decode() or "{}")
    except Exception:
        return None
    finally:
        conn.close()


def serve_health(h: dict) -> tuple[bool, str]:
    j = http_json(h['ports']['serve'], "/health")
    return (bool(j) and j.get("engine") == "postgres" and j.get("status") == "ok",
            f"serve :{h['ports']['serve']}/health {json.dumps(j) if j else 'no answer'}")


def wait_for(pred: Callable[[], tuple[bool, str]], timeout: float, every: float = 0.5) -> tuple[bool, str]:
    end = time.monotonic() + timeout
    ok, text = pred()
    while not ok and time.monotonic() < end:
        time.sleep(every)
        ok, text = pred()
    return ok, text


def launchd_state(label: str) -> str:
    rc, out = sh("launchctl", "print", f"gui/{os.getuid()}/{label}")
    if rc != 0:
        return "not-loaded"
    for line in out.splitlines():
        if line.strip().startswith("state ="):
            return line.split("=", 1)[1].strip()
    return "loaded"


def launchd_disabled(label: str) -> bool | None:
    rc, out = sh("launchctl", "print-disabled", f"gui/{os.getuid()}")
    if rc != 0:
        return None
    for line in out.splitlines():
        if f'"{label}"' in line:
            return "disabled" in line or "true" in line
    return False


def cron_jobs(ids: tuple[str, ...]) -> dict[str, dict | None]:
    """The full cron rows for `ids` from `hermes cron list --all --json` (None = missing or unreadable)."""
    rc, out = sh("hermes", "cron", "list", "--all", "--json", timeout=60)
    found: dict[str, dict | None] = {i: None for i in ids}
    if rc != 0:
        return found
    try:
        d = json.loads(out)
    except ValueError:
        return found
    for j in (d.get("jobs", []) if isinstance(d, dict) else d):
        if isinstance(j, dict) and j.get("id") in found:
            found[j["id"]] = j
    return found


def cron_rows(ids: tuple[str, ...]) -> dict[str, tuple[bool, str] | None]:
    return {i: None if j is None else (bool(j.get("enabled")), str(j.get("state"))) for i, j in cron_jobs(ids).items()}


# The cron store's write guard (hermes-home scripts/cron-write-guard -> cron_write_guard.py, which runs
# scripts/lint_one_sender_crons.py on the candidate row). `hermes cron resume` passes every enabled row through it.
CRON_WRITE_GUARD = Path("~/.hermes/scripts/cron-write-guard")


def cron_resume_accepted(ids: tuple[str, ...], guard: Path | None = None) -> tuple[bool, str]:
    """Would `hermes cron resume` be accepted for each row? Asks the store's own write guard with the row as it
    would be after the resume (enabled). Read-only: the guard grades the candidate in a temp dir and writes nothing."""
    g = Path(os.path.expanduser(str(guard or CRON_WRITE_GUARD)))
    if not os.access(g, os.X_OK):
        return False, f"{g} missing: cannot ask the cron write guard"
    parts, ok = [], True
    for i, j in cron_jobs(ids).items():
        if j is None:
            parts.append(f"{i}=missing")
            ok = False
            continue
        cand = dict(j, enabled=True, state="scheduled", paused_at=None, paused_reason=None)
        cand.pop("latest_execution", None)
        rc, out = sh(str(g), "update", timeout=60, input=json.dumps(cand))
        parts.append(f"{i}=" + ("accepted" if rc == 0 else f"REFUSED rc={rc}: {out[-300:]}"))
        ok &= rc == 0
    return ok, " ".join(parts)


def cron_state_ok(want: dict[str, bool]) -> tuple[bool, str]:
    rows = cron_rows(tuple(want))
    parts, ok = [], True
    for i, en in want.items():
        r = rows.get(i)
        parts.append(f"{i}={'missing' if r is None else ('enabled' if r[0] else 'paused') + '/' + r[1]}")
        ok &= r is not None and r[0] is en
    return ok, " ".join(parts)
