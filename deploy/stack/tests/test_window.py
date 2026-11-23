"""deploy/stack/tests/test_window.py: gbrain-cutover / gbrain-rollback / window_lib and the stack watch's parity rule
(card t_bb0e9194).   python3 -m pytest deploy/stack/tests -q

  - a dry run runs no step command (only readbacks); --apply runs them and stops on a readback that is not ok
  - --drill skips every production host-state step and both scripts refuse the wrong project/mode pairing
  - the cutover's C4 (restart policy no) precedes C5 (stop), and its C6 does not re-render inside the downtime
  - the rollback's launchd step is queued through detached-run.py, never a launchctl call in the process
  - gbrain-stack-watch's parity accepts a master that moved past the pin only in host tooling (the pin PR itself)
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

STACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STACK))
import window_lib as W  # noqa: E402


def load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cutover = load("gbrain_cutover", STACK / "gbrain-cutover")
rollback = load("gbrain_rollback", STACK / "gbrain-rollback")


def test_dry_run_runs_no_command_apply_does(tmp_path, capsys):
    marker = tmp_path / "ran"
    step = W.Step("C1", "touch", cmds=[["touch", str(marker)]],
                  readback=lambda: (marker.exists(), f"exists={marker.exists()}"))
    assert W.Window("t", apply=False, drill=False).run_steps([step]) == 0
    assert not marker.exists()
    assert "readback now (before the step): not yet" in capsys.readouterr().out
    assert W.Window("t", apply=True, drill=False).run_steps([step]) == 0
    assert marker.exists()
    assert W.Window("t", apply=True, drill=False).run_steps([step]) == 0      # second apply: already
    assert "already: exists=True" in capsys.readouterr().out


def test_apply_stops_on_bad_readback_and_later_steps_do_not_run(tmp_path, capsys):
    later = tmp_path / "later"
    steps = [W.Step("C1", "bad", cmds=[["true"]], readback=lambda: (False, "nope")),
             W.Step("C2", "later", cmds=[["touch", str(later)]])]
    assert W.Window("t", apply=True, drill=False).run_steps(steps) == 1
    out = capsys.readouterr().out
    assert "STOPPED: C1 readback not ok: nope" in out and "RESULT=stopped" in out and not later.exists()


def test_preflight_fails_dry_run_rc1_but_prints_every_step(capsys):
    steps = [W.Step("P1", "probe", readback=lambda: (False, "no"), readonly=True), W.Step("C1", "next")]
    assert W.Window("t", apply=False, drill=False).run_steps(steps) == 1
    out = capsys.readouterr().out
    assert "== C1 next" in out and "RESULT=preflight-fail(P1)" in out


def test_dry_run_skips_a_state_writing_readback(tmp_path, capsys):
    marker = tmp_path / "state"
    s = W.Step("C14", "watch", readback=lambda: (marker.touch() or True, "x"), dry_readback=False)
    assert W.Window("t", apply=False, drill=False).run_steps([s]) == 0
    assert not marker.exists() and "only under --apply" in capsys.readouterr().out


def test_drill_skips_prod_only_steps(tmp_path, capsys):
    marker = tmp_path / "ran"
    s = W.Step("C2", "launchd", cmds=[["touch", str(marker)]], prod_only=True)
    assert W.Window("t", apply=True, drill=True).run_steps([s]) == 0
    assert not marker.exists() and "SKIP (drill" in capsys.readouterr().out


def test_downtime_is_measured_between_marked_steps(capsys):
    steps = [W.Step("C5", "stop", downtime_start=True), W.Step("C6", "up", downtime_end=True)]
    assert W.Window("t", apply=True, drill=False).run_steps(steps) == 0
    assert "DOWNTIME_S=" in capsys.readouterr().out


def test_gbrainctl_host_args_go_before_the_compose_separator():
    h = {"_hostargs": ["--host", "studio"]}
    argv = W.gbrainctl(h, "compose", "--allow-prod", "--", "up", "-d")
    assert argv[2:] == ["compose", "--allow-prod", "--host", "studio", "--", "up", "-d"]
    assert W.gbrainctl(h, "status")[2:] == ["status", "--host", "studio"]


def _host(**kw):
    h = W.load_host("studio", None, "/tmp/nowhere")
    h.update(kw)
    return h


def test_cutover_order_and_prod_only_set():
    st = cutover.steps(_host(), drill=False)
    ids = [s.sid for s in st]
    assert ids.index("C4") < ids.index("C5") < ids.index("C6")
    assert {s.sid for s in st if s.prod_only} == {"P4", "P5", "C1", "C2", "C3", "C9", "C10", "C12", "C13", "C14"}
    by = {s.sid: s for s in st}
    assert by["C4"].cmds == [["docker", "update", "--restart=no", "gbrain-pg"]]
    assert by["C5"].cmds[0] == ["docker", "stop", "-t", "60", "gbrain-pg"]
    assert by["C2"].downtime_start and by["C6"].downtime_end
    assert by["C6"].cmds[0][-3:] == ["--", "up", "-d"] and "up" not in by["C6"].cmds[0][2:3]   # no re-render
    assert by["C11"].always
    assert by["C14"].always                    # one readback call (Prism 5fd839303bc5)
    drill = {s.sid: s for s in cutover.steps(_host(), drill=True)}
    assert drill["C5"].downtime_start and not drill["C2"].downtime_start


def test_ssh_probes_ignore_host_ssh_config(monkeypatch):
    # a host ~/.ssh/config ControlMaster would take P6's forward (ssh exits 0 at once) and prove the master's key
    seen = []

    class FakePopen:
        returncode = 0

        def __init__(self, argv, **kw):
            seen.append(argv)

        def poll(self):
            return 0

        def terminate(self):
            pass

        stderr = type("E", (), {"read": staticmethod(lambda: "")})()

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cutover.W, "sh", lambda *argv, **kw: (seen.append(list(argv)), (0, ""))[1])
    by = {s.sid: s for s in cutover.steps(_host(), drill=False)}
    by["P6"].readback()
    by["P7"].readback()
    assert [a[0] for a in seen] == ["ssh", "sftp"]
    for argv in seen:
        assert argv[1:3] == ["-F", "/dev/null"]
        assert "IdentityAgent=none" in argv and "IdentitiesOnly=yes" in argv and "StrictHostKeyChecking=yes" in argv


def test_rollback_launchd_step_is_detached():
    st = {s.sid: s for s in rollback.steps(_host(), drill=False)}
    assert not st["R4"].cmds and st["R4"].run is not None and st["R4"].prod_only
    assert any("detached-run.py" in c and "rollback-launchd.sh" in c for c in st["R4"].show)
    src = (STACK / "gbrain-rollback").read_text()
    assert '"launchctl", "bootstrap"' not in src and "launchctl bootstrap" in (STACK / "rollback-launchd.sh").read_text()
    order = list(st)
    assert order.index("R1") < order.index("R2") < order.index("R3") < order.index("R4") < order.index("R5")
    r = subprocess.run(["bash", "-n", str(STACK / "rollback-launchd.sh")])
    assert r.returncode == 0


@pytest.mark.parametrize("script", ["gbrain-cutover", "gbrain-rollback"])
def test_scripts_refuse_wrong_mode_pairing(script, tmp_path):
    hf = tmp_path / "d.toml"
    hf.write_text((STACK / "hosts" / "studio.toml").read_text().replace('compose_project = "gbrain"',
                                                                         'compose_project = "gbrain-drill"'))
    run = lambda *a: subprocess.run([sys.executable, str(STACK / script), *a], capture_output=True, text=True)  # noqa
    assert run("--host-file", str(hf)).returncode == 2                      # scratch project without --drill
    assert run("--host", "studio", "--drill").returncode == 2               # production with --drill


@pytest.mark.parametrize("script", ["gbrain-cutover", "gbrain-rollback"])
def test_drill_refuses_a_profile_naming_production_targets(script, tmp_path):
    """Prism e8731ada0c50: a scratch project whose pg container / volume / ports are production's is refused before
    any step runs (C4/C5 act on pg_container outside compose)."""
    hf = tmp_path / "d.toml"
    hf.write_text((STACK / "hosts" / "studio.toml").read_text().replace('compose_project = "gbrain"',
                                                                         'compose_project = "gbrain-drill"'))
    r = subprocess.run([sys.executable, str(STACK / script), "--host-file", str(hf), "--drill"],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "pg_container=gbrain-pg" in r.stderr and "ports.serve=8199" in r.stderr, r.stderr
    assert W.drill_clashes(W.load_host("studio-proof", None, "/tmp/nowhere")) == []


def test_c14_runs_the_watch_once_in_check_mode(monkeypatch, capsys):
    """Prism 5fd839303bc5: C14 calls the watch once, with --check; a finding stops the window."""
    calls = []
    monkeypatch.setattr(W, "sh", lambda *a, **k: (calls.append(a), (1, "parity finding"))[1])
    c14 = next(s for s in cutover.steps(_host(), drill=False) if s.sid == "C14")
    assert W.Window("t", apply=True, drill=False).run_steps([c14]) == 1
    assert len(calls) == 1 and "--check" in calls[0]
    assert "STOPPED: C14 readback not ok" in capsys.readouterr().out


def test_rollback_r4_does_not_wait_on_the_rerank_primary(monkeypatch):
    """Prism ab8e60469570: launchd back + serve ok + rerank answering is enough; primary_ok=false is advisory."""
    monkeypatch.setattr(W, "launchd_state", lambda lab: "running")
    monkeypatch.setattr(W, "serve_health", lambda h: (True, "serve ok"))
    monkeypatch.setattr(W, "http_json", lambda port, path="/health", timeout=3: {"primary_ok": False})
    st = {s.sid: s for s in rollback.steps(_host(), drill=False)}
    ok, text = st["R4"].readback()
    assert ok and "ADVISORY" in text
    monkeypatch.setattr(W, "http_json", lambda port, path="/health", timeout=3: None)
    assert st["R4"].readback()[0] is False


# ---- gbrain-stack-watch parity: the pin PR's own commit is master, not the pin -----------------------------------
def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "src").mkdir()
    (r / "src" / "a.ts").write_text("1")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "pin")
    _git(r, "remote", "add", "origin", str(r))
    return r


def _commit(r, path, text="x"):
    p = r / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", path)
    return _git(r, "rev-parse", "HEAD")


def test_parity_host_tooling_only(repo):
    watch = load("gbrain_stack_watch_t", STACK / "gbrain-stack-watch")
    pin = _git(repo, "rev-parse", "HEAD")
    p = _commit(repo, "deploy/stack/hosts/studio.toml")
    p = _commit(repo, "deploy/stack/gbrain-cutover") and _commit(repo, "deploy/stack/README.md")
    assert watch.host_tooling_only(pin, p, repo)
    app = _commit(repo, "src/a.ts", "2")
    assert not watch.host_tooling_only(pin, app, repo)
    img = _commit(repo, "deploy/stack/cron/jobs/x.sh")
    assert not watch.host_tooling_only(p, img, repo)
    assert not watch.host_tooling_only(app, pin, repo)                         # master behind the pin
    assert not watch.host_tooling_only(pin, "f" * 40, repo)                    # unknown sha: fetch fails
