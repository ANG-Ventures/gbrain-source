"""deploy/stack/tests/test_stack.py: static contract of the gbrain compose stack (card t_ab2fcba3).

  python3 -m pytest deploy/stack/tests -q        (stdlib + pytest; docker optional: the compose check skips without it)

Pins what the proof relied on, so a later edit cannot silently break it:
  - the crontab and gbrainjob's JOBS table agree line for line, and every job has a script
  - every compose secret is a schema [[file]] and the reverse (render writes exactly what compose mounts)
  - render is deterministic and fails closed (a <placeholder> pin, a missing 1P value), never prints a value
  - the rendered config.json is the live ~/gbrain/.gbrain/config.json with only the reranker URL moved in-stack
  - `docker compose config` accepts the rendered env for every host profile
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
from pathlib import Path

import pytest

STACK = Path(__file__).resolve().parents[1]
FAKE_OP = """#!/bin/sh
case "$1" in
  *notesPlain) printf 'NAS_SSH_KEY_B64=a2V5Cg==\\nNAS_KNOWN_HOSTS_B64=a2gK\\nTUNNEL_SSH_KEY_B64=a2V5Cg==\\nTUNNEL_KNOWN_HOSTS_B64=a2gK\\n' ;;
  *MISSING*) echo "op-cached-read ERROR: not found" >&2; exit 1 ;;
  *) printf 'SECRETVALUE-%s' "$(printf %s "$1" | cksum | cut -d' ' -f1)" ;;
esac
"""


def load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


gbrainjob = load("gbrainjob", STACK / "cron" / "gbrainjob")
gbrainctl = load("gbrainctl", STACK / "gbrainctl")


def test_crontab_matches_jobs_table():
    lines = [l.split() for l in (STACK / "cron" / "crontab").read_text().splitlines()
             if l.strip() and not l.startswith("#")]
    tab = {l[7]: " ".join(l[:5]) for l in lines}
    assert all(l[5:7] == ["gbrainjob", "tick"] for l in lines)
    assert tab == {j: s["cron"] for j, s in gbrainjob.JOBS.items()}
    for j in gbrainjob.JOBS:
        assert (STACK / "cron" / "jobs" / f"{j}.sh").is_file(), j


def test_compose_secrets_equal_schema_files():
    schema = tomllib.loads((STACK / "env.schema.toml").read_text())
    files = {f["path"] for f in schema["file"]}
    compose = (STACK / "docker-compose.yaml").read_text()
    top = compose.split("\nsecrets:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    declared = set(re.findall(r"^  ([a-z0-9.-]+):$", top, re.M))
    paths = set(re.findall(r"file: \./secrets/([a-z0-9.-]+)", top))
    assert declared == files == paths


@pytest.fixture()
def fake_op(tmp_path, monkeypatch):
    p = tmp_path / "op"
    p.write_text(FAKE_OP)
    p.chmod(0o755)
    monkeypatch.setenv("GBRAINCTL_OP_READER", str(p))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))      # render mkdirs the host's receipts dirs under ~
    return p


def render(host: str, stack: Path, capsys, extra=()):
    rc = gbrainctl.main(["render", "--host-file", str(STACK / "hosts" / f"{host}.toml"), "--stack-dir", str(stack),
                         *extra])
    return rc, capsys.readouterr()


def test_render_proof_is_deterministic_and_secret_free_on_stdout(tmp_path, fake_op, capsys):
    rc, out = render("studio-proof", tmp_path, capsys)
    assert rc == 0 and "RENDER=ok host=studio-proof" in out.out
    assert "SECRETVALUE" not in out.out + out.err
    env1 = (tmp_path / ".env").read_bytes()
    rc, out = render("studio-proof", tmp_path, capsys)
    assert rc == 0 and "changed=0" in out.out
    assert (tmp_path / ".env").read_bytes() == env1
    assert oct((tmp_path / ".env").stat().st_mode & 0o777) == "0o600"
    assert (tmp_path / "secrets" / "tunnel-ssh-key").read_bytes() == b""      # feature off -> empty, no 1P read
    assert (tmp_path / "secrets" / "openai-key").read_text().startswith("SECRETVALUE-")


def test_render_studio_tunnel_and_nas_files_are_decoded(tmp_path, fake_op, capsys):
    h = (STACK / "hosts" / "studio.toml").read_text()
    if "<" in h.split("revision", 1)[1].split("\n", 1)[0]:
        pinned = re.sub(r'(revision\s*=\s*)"[^"]*"', r'\1"' + "a" * 40 + '"', h)
        pinned = re.sub(r'(_image\s*=\s*"gbrain-[a-z]+:)[^"]*"', r'\1aaaaaaaaaaaa-arm64"', pinned)
        hf = tmp_path / "studio.toml"
        hf.write_text(pinned)
        rc = gbrainctl.main(["render", "--host-file", str(hf), "--stack-dir", str(tmp_path / "s")])
        stack = tmp_path / "s"
    else:
        rc, _ = render("studio", tmp_path / "s", capsys)
        stack = tmp_path / "s"
    assert rc == 0
    assert (stack / "secrets" / "tunnel-ssh-key").read_bytes() == b"key\n"
    assert (stack / "secrets" / "nas-known-hosts").read_bytes() == b"kh\n"
    assert "COMPOSE_PROFILES=tunnel\n" in (stack / ".env").read_text()


def test_render_refuses_placeholder_pin(tmp_path, fake_op, capsys):
    h = (STACK / "hosts" / "studio-proof.toml").read_text()
    bad = re.sub(r'(revision\s*=\s*)"[^"]*"', r'\1"<merge sha>"', h)
    hf = tmp_path / "bad.toml"
    hf.write_text(bad)
    with pytest.raises(SystemExit) as e:
        gbrainctl.main(["render", "--host-file", str(hf), "--stack-dir", str(tmp_path / "s")])
    assert e.value.code == 2
    assert "GBRAIN_REVISION" in capsys.readouterr().err


def test_render_fails_closed_on_missing_required_secret(tmp_path, fake_op, capsys, monkeypatch):
    schema = (STACK / "env.schema.toml").read_text().replace("zizaufjihwcwxk6am5rga5xpgm", "MISSING")
    alt = tmp_path / "schema.toml"
    alt.write_text(schema)
    monkeypatch.setattr(gbrainctl, "SCHEMA", alt)
    with pytest.raises(SystemExit):
        render("studio-proof", tmp_path / "s", capsys)
    err = capsys.readouterr().err
    assert "secrets/openai-key" in err and "SECRETVALUE" not in err


def test_config_json_is_live_config_with_instack_reranker(tmp_path, fake_op, capsys):
    render("studio-proof", tmp_path, capsys)
    cfg = json.loads((tmp_path / "secrets" / "config.json").read_text())
    assert cfg == {"engine": "postgres", "embedding_model": "openai:text-embedding-3-large",
                   "embedding_dimensions": 1536, "schema_pack": "gbrain-base-v2", "mcp": {"publish_skills": True},
                   "self_upgrade": {"mode": "off", "mode_prompted": True},
                   "link_resolution": {"global_basename": True},
                   "provider_base_urls": {"llama-server-reranker": "http://rerank-proxy:18085/v1"}}


def test_compose_refuses_global_flag_and_unrendered_env(tmp_path, capsys):
    (tmp_path / ".env").write_text("FOO=1\n")
    with pytest.raises(SystemExit):
        gbrainctl.main(["compose", "--host", "studio-proof", "--stack-dir", str(tmp_path), "--", "-p", "x", "ps"])
    with pytest.raises(SystemExit):
        gbrainctl.main(["compose", "--host", "studio-proof", "--stack-dir", str(tmp_path), "--", "ps"])
    assert "not a rendered file" in capsys.readouterr().err


def test_up_refuses_prod_without_allow_prod(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        gbrainctl.main(["up", "--host-file", str(STACK / "hosts" / "studio.toml"), "--stack-dir", str(tmp_path)])
    assert e.value.code == 3


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
@pytest.mark.parametrize("host", ["studio-proof"])
def test_compose_config_accepts_rendered_env(tmp_path, fake_op, capsys, host):
    stack = tmp_path / "stack"
    shutil.copytree(STACK, stack, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    render(host, stack, capsys)
    h = tomllib.loads((STACK / "hosts" / f"{host}.toml").read_text())
    argv = gbrainctl.compose_argv({**h, "_file": host}, stack) + ["config", "-q"]
    r = subprocess.run(argv, capture_output=True, text=True)
    if "docker compose" in r.stderr and "not a docker command" in r.stderr:
        pytest.skip("docker compose plugin missing")
    assert r.returncode == 0, r.stderr[-400:]


# ---- round 1 (Apollo send-back 2026-11-21): the prod guard covers every compose verb ---------------------------
FAKE_DOCKER = """#!/bin/sh
# gbrainctl runs docker with a clean env (PATH, HOME), so the fake reads its knobs from files beside it:
# records argv in docker.log; `compose ... ps` prints ps-rows and exits ps-rc; `inspect` prints label
d=$(dirname "$0")
printf '%s\\n' "$*" >> "$d/docker.log"
case "$*" in
  inspect*) cat "$d/label" 2>/dev/null; echo ;;
  *" ps "*) cat "$d/ps-rows" 2>/dev/null; exit "$(cat "$d/ps-rc" 2>/dev/null || echo 0)" ;;
esac
"""


@pytest.fixture()
def prod_stack(tmp_path, monkeypatch):
    """A host file for the production project `gbrain`, a rendered-looking .env and a fake docker on PATH."""
    h = (STACK / "hosts" / "studio-proof.toml").read_text().replace('"gbrain-proof"', '"gbrain"', 1)
    assert 'compose_project = "gbrain"' in h
    hf = tmp_path / "prod.toml"
    hf.write_text(h)
    stack = tmp_path / "stack"
    stack.mkdir()
    (stack / ".env").write_text("# RENDERED by gbrainctl abc schema=def from hosts/studio-proof.toml; do not edit\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(FAKE_DOCKER)
    (bindir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return hf, stack, bindir / "docker.log"


MUTATING = ["down", "stop", "rm", "restart", "kill", "up", "start", "exec", "run", "create", "pull", "build", "pause"]
READ_ONLY = ["ps", "config", "logs"]


@pytest.mark.parametrize("verb", MUTATING)
def test_compose_refuses_mutating_verb_on_prod_without_allow_prod(prod_stack, capsys, verb):
    hf, stack, log = prod_stack
    with pytest.raises(SystemExit) as e:
        gbrainctl.main(["compose", "--host-file", str(hf), "--stack-dir", str(stack), "--", verb])
    assert e.value.code == 3
    assert f"refusing `compose {verb}`" in capsys.readouterr().err
    assert not log.exists()                                    # docker never ran


@pytest.mark.parametrize("verb", MUTATING)
def test_compose_runs_mutating_verb_on_prod_with_allow_prod(prod_stack, verb):
    hf, stack, log = prod_stack
    rc = gbrainctl.main(["compose", "--host-file", str(hf), "--stack-dir", str(stack), "--allow-prod", "--", verb])
    assert rc == 0 and log.read_text().split("\n")[0].endswith(f" {verb}")


@pytest.mark.parametrize("verb", READ_ONLY)
@pytest.mark.parametrize("allow", [False, True])
def test_compose_read_only_verbs_need_no_allow_prod(prod_stack, verb, allow):
    hf, stack, log = prod_stack
    rc = gbrainctl.main(["compose", "--host-file", str(hf), "--stack-dir", str(stack)]
                        + (["--allow-prod"] if allow else []) + ["--", verb])
    assert rc == 0 and log.read_text().split("\n")[0].endswith(f" {verb}")


def test_compose_non_prod_project_needs_no_allow_prod(prod_stack, tmp_path):
    _, stack, log = prod_stack
    rc = gbrainctl.main(["compose", "--host-file", str(STACK / "hosts" / "studio-proof.toml"), "--stack-dir",
                         str(stack), "--", "down"])
    assert rc == 0 and "-p gbrain-proof" in log.read_text()


# ---- Prism 4cb1b0a15a94: status is not ok on an unlabelled image, a failed ps, or no containers ----------------
def _status(prod_stack, monkeypatch, capsys, rows, label, ps_rc=0):
    hf, stack, log = prod_stack
    (log.parent / "ps-rows").write_text(rows)
    (log.parent / "label").write_text(label)
    (log.parent / "ps-rc").write_text(str(ps_rc))
    rc = gbrainctl.main(["status", "--host-file", str(hf), "--stack-dir", str(stack)])
    return rc, capsys.readouterr().out


def test_status_ok_only_when_every_built_service_carries_the_pin(prod_stack, monkeypatch, capsys):
    pin = tomllib.loads((STACK / "hosts" / "studio-proof.toml").read_text())["revision"]
    rows = "gbrain-pg postgres pgvector Up\ngbrain-server-1 server gbrain-server:x Up\n"
    rc, out = _status(prod_stack, monkeypatch, capsys, rows, pin)
    assert rc == 0 and "STATUS=ok" in out
    rc, out = _status(prod_stack, monkeypatch, capsys, rows, "")          # unlabelled server image
    assert rc == 1 and "STATUS=drift" in out
    rc, out = _status(prod_stack, monkeypatch, capsys, "", pin)           # no containers at all
    assert rc == 1 and "STATUS=drift" in out
    rc, out = _status(prod_stack, monkeypatch, capsys, "", pin, ps_rc=1)  # compose ps failed, no rows
    assert rc == 1 and "compose ps failed rc=1" in out


# ---- Prism 85fcff4bc647: an .mdx-only night is not "nothing new" ------------------------------------------------
@pytest.mark.parametrize("newfile,expect", [("vault/a.mdx", "skipped_audit_lock"), ("vault/a.md", "skipped_audit_lock"),
                                            (None, "nothing_new")])
def test_nightly_gate_counts_every_import_extension(tmp_path, newfile, expect):
    gb, st, lib = tmp_path / "gb", tmp_path / "state", tmp_path / "lib"
    (gb / "brain" / "vault").mkdir(parents=True)
    st.mkdir()
    lib.mkdir()
    shutil.copy(STACK / "cron" / "lib" / "import-exts.sh", lib)
    (lib / "audit_lock.py").write_text("import sys; sys.exit(75)\n")   # lock held: the run stops right after the gate
    old = gb / "brain" / "vault" / "old.md"
    old.write_text("x")
    stamp = st / "last-nightly-sync"
    stamp.write_text("")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(stamp, (2_000_000, 2_000_000))
    if newfile:
        (gb / "brain" / newfile).write_text("new")                  # mtime now: newer than the stamp
    env = {**os.environ, "GBRAIN_HOME": str(gb), "GBRAIN_STATE_DIR": str(st), "GBRAIN_CRON_LIB": str(lib),
           "GBRAIN_NIGHTLY_ROOTS": "vault"}
    r = subprocess.run(["bash", str(STACK / "cron" / "jobs" / "nightly-sync.sh")], env=env, capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert expect in r.stdout, r.stdout


def test_gate_and_import_share_one_extension_list():
    sync = (STACK / "cron" / "jobs" / "nightly-sync.sh").read_text()
    ingest = (STACK / "cron" / "lib" / "ingest-chunked.sh").read_text()
    assert '. "$LIB/import-exts.sh"' in sync and '/import-exts.sh"' in ingest
    for text in (sync, ingest):
        assert '"*.md"' not in text and '".mdx"' not in text and "'.mdx'" not in text


# ---- semgrep missing-user: every stack image ends on a non-root USER -----------------------------------------
@pytest.mark.parametrize("img", ["server", "rerank", "cron"])
def test_images_run_as_the_stack_uid(img):
    lines = [l.strip() for l in (STACK / img / "Dockerfile").read_text().splitlines()]
    users = [l.split(None, 1)[1] for l in lines if l.startswith("USER ")]
    assert users and users[-1] == "${GBRAIN_UID}:${GBRAIN_GID}"
    assert lines.index(next(l for l in lines if l.startswith("USER ${GBRAIN_UID}"))) < max(
        i for i, l in enumerate(lines) if l.startswith(("ENTRYPOINT", "CMD")))


# ---- Prism 79c5595441c1: a forwarded --project-name cannot retarget compose past the guard -------------------
@pytest.mark.parametrize("args", [["up", "--project-name=gbrain", "--dry-run"], ["up", "--project-name", "gbrain"],
                                  ["down", "-p", "gbrain"], ["down", "-pgbrain"], ["up", "-dp", "gbrain"]])
@pytest.mark.parametrize("allow", [False, True])
def test_compose_refuses_forwarded_project_override(prod_stack, capsys, args, allow):
    _, stack, log = prod_stack
    with pytest.raises(SystemExit) as e:
        gbrainctl.main(["compose", "--host-file", str(STACK / "hosts" / "studio-proof.toml"), "--stack-dir", str(stack)]
                       + (["--allow-prod"] if allow else []) + ["--"] + args)
    assert e.value.code == 64
    assert "refusing compose project override" in capsys.readouterr().err
    assert not log.exists()                                    # docker never ran


def test_updown_refuses_forwarded_project_override(prod_stack, capsys):
    _, stack, log = prod_stack
    with pytest.raises(SystemExit) as e:
        gbrainctl.main(["down", "--host-file", str(STACK / "hosts" / "studio-proof.toml"), "--stack-dir", str(stack),
                        "--", "--project-name=gbrain"])
    assert e.value.code == 64 and not log.exists()


# ---- Prism 7945fb55fb98: the rerank proxy never follows a redirect (the bearer would go with it) --------------
def test_rerank_proxy_refuses_redirects(monkeypatch):
    import http.server
    import threading
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("RERANK_PRIMARY_BASE", base)
    monkeypatch.setenv("DEEPINFRA_KEY_FILE", "/nonexistent")
    proxy = load("rerank_proxy", STACK / "rerank" / "proxy.py")
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            proxy._post(base + "/v1/rerank", {"q": 1}, {"Authorization": "Bearer t"}, 5)
        assert e.value.code == 302
        assert seen == [("/v1/rerank", "Bearer t")]              # the redirect target was never requested
    finally:
        srv.shutdown()


# ---- Prism d41bb2babef3: rows already in the DB do not complete a root whose import pass failed ---------------
def _ingest(tmp_path, rcs, max_passes):
    lib = tmp_path / "lib"
    lib.mkdir()
    for f in ("ingest-chunked.sh", "import-exts.sh"):
        shutil.copy(STACK / "cron" / "lib" / f, lib)
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "rcs").write_text("\n".join(rcs) + "\n")
    fake = tmp_path / "fake-gbrain"
    fake.write_text(f'#!/bin/bash\nn=$(cat {tmp_path}/calls 2>/dev/null | wc -l); echo x >> {tmp_path}/calls\n'
                    f'exit "$(sed -n "$((n + 1))p" {tmp_path}/rcs)"\n')
    fake.chmod(0o755)
    h = lib / "harness.sh"
    h.write_text('INGEST_CHUNKED_SOURCE_ONLY=1 . "$(dirname "$0")/ingest-chunked.sh"\n'
                 'db_engine() { echo postgres; }\nreadback() { echo "1 1"; }\nmain "$1"\n')
    env = {**os.environ, "GBRAIN_DIR": str(tmp_path), "GBRAIN_CMD": str(fake), "INGEST_LOG": str(tmp_path / "log"),
           "WATCHDOG_POLL_S": "0.1", "PASS_GAP_S": "0", "MAX_PASSES": str(max_passes)}
    r = subprocess.run(["bash", str(h), str(root)], env=env, capture_output=True, text=True, timeout=60)
    return r.returncode, len((tmp_path / "calls").read_text().split()), (tmp_path / "log").read_text()


def test_ingest_failed_pass_with_full_readback_runs_another_pass(tmp_path):
    rc, calls, log = _ingest(tmp_path, ["1", "0"], 8)
    assert (rc, calls) == (0, 2), log
    assert "COMPLETE after pass 2" in log


def test_ingest_never_completes_on_readback_alone(tmp_path):
    rc, calls, log = _ingest(tmp_path, ["1", "1"], 2)
    assert (rc, calls) == (1, 2), log
    assert "COMPLETE" not in log


# ---- Prism 5c4347a17373: a git failure fails the night and leaves the stamp where it was -----------------------
def _night(tmp_path, lock_index):
    gb, st, lib, bindir = tmp_path / "gb", tmp_path / "state", tmp_path / "lib", tmp_path / "bin"
    for d in (gb / "brain" / "vault", st, lib, bindir):
        d.mkdir(parents=True)
    shutil.copy(STACK / "cron" / "lib" / "import-exts.sh", lib)
    (lib / "audit_lock.py").write_text("import sys; sys.exit(0)\n")
    (lib / "ingest-chunked.sh").write_text("exit 0\n")
    brain = gb / "brain"
    subprocess.run(["git", "init", "-q", str(brain)], check=True)
    (brain / "vault" / "a.md").write_text("new")
    if lock_index:
        (brain / ".git" / "index.lock").write_text("")
    stamp = st / "last-nightly-sync"
    stamp.write_text("")
    os.utime(stamp, (1_000_000, 1_000_000))
    # /usr/local/bin/gbrain extract-links is absent off-image: the script treats it as non-fatal
    env = {**os.environ, "GBRAIN_HOME": str(gb), "GBRAIN_STATE_DIR": str(st), "GBRAIN_CRON_LIB": str(lib),
           "GBRAIN_NIGHTLY_ROOTS": "vault"}
    r = subprocess.run(["bash", str(STACK / "cron" / "jobs" / "nightly-sync.sh")], env=env, capture_output=True,
                       text=True)
    return r, stamp.stat().st_mtime


def test_nightly_git_failure_fails_and_keeps_stamp(tmp_path):
    r, mtime = _night(tmp_path, lock_index=True)
    assert r.returncode == 1 and "git add" in r.stdout, r.stdout + r.stderr
    assert mtime == 1_000_000


def test_nightly_commit_advances_stamp(tmp_path):
    r, mtime = _night(tmp_path, lock_index=False)
    assert r.returncode == 0 and '"brain_committed": 1' in r.stdout, r.stdout + r.stderr
    assert mtime > 1_000_000


# ---- t_d3252f19 fix 1: a .env rendered for another host is refused (compose/up/down/status, and render over it) --
OTHER_HEADER = "# RENDERED by gbrainctl abc schema=def from hosts/studio.toml; do not edit\n"


@pytest.mark.parametrize("argv", [["compose", "--", "ps"], ["compose", "--", "up"], ["down"], ["status"]])
def test_compose_verbs_refuse_env_rendered_for_another_host(prod_stack, capsys, argv):
    _, stack, log = prod_stack
    (stack / ".env").write_text(OTHER_HEADER)                  # the prod stack dir; the host below is studio-proof
    with pytest.raises(SystemExit) as e:
        gbrainctl.main([argv[0], "--host-file", str(STACK / "hosts" / "studio-proof.toml"), "--stack-dir", str(stack)]
                       + argv[1:])
    assert e.value.code == 3
    assert "was rendered for host 'studio'" in capsys.readouterr().err
    assert not log.exists()                                    # docker never ran


def test_render_refuses_to_overwrite_another_hosts_env(tmp_path, fake_op, capsys):
    stack = tmp_path / "prod"
    stack.mkdir()
    (stack / ".env").write_text(OTHER_HEADER + "GBRAIN_PG_CONTAINER=gbrain-pg\n")
    (stack / "secrets").mkdir()
    (stack / "secrets" / "openai-key").write_text("PROD")
    with pytest.raises(SystemExit) as e:
        render("studio-proof", stack, capsys)
    assert e.value.code == 2
    assert "was rendered for host 'studio'" in capsys.readouterr().err
    assert (stack / ".env").read_text() == OTHER_HEADER + "GBRAIN_PG_CONTAINER=gbrain-pg\n"
    assert (stack / "secrets" / "openai-key").read_text() == "PROD"
    rc, out = render("studio-proof", tmp_path / "proof", capsys)   # its own (or an empty) stack dir still renders
    assert rc == 0 and "RENDER=ok host=studio-proof" in out.out
    rc, out = render("studio-proof", tmp_path / "proof", capsys)
    assert rc == 0 and "changed=0" in out.out


# ---- t_d3252f19 fixes 2 + 3: the runbook's restart-policy step and the rollback readbacks ------------------------
def _readme_section(title: str) -> str:
    text = (STACK / "README.md").read_text()
    return text.split(f"## {title}", 1)[1].split("\n## ", 1)[0]


def test_cutover_sets_old_pg_restart_policy_to_no_before_the_stop():
    cut = _readme_section("Cutover")
    upd = cut.index("docker update --restart=no gbrain-pg")
    readback = cut.index("docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' gbrain-pg", upd)
    assert upd < readback < cut.index("docker stop -t 60 gbrain-pg")
    rb = _readme_section("Rollback")
    assert "docker update --restart=unless-stopped gbrain-pg" in rb
    assert rb.index("--restart=unless-stopped") < rb.index("RestartPolicy.Name")


def test_rollback_pauses_the_watch_first_and_reads_back_every_step():
    rb = _readme_section("Rollback")
    pause = rb.index("hermes cron pause <gbrain-stack-watch row>")
    assert pause < rb.index("gbrainctl down")
    for readback in ("label=com.docker.compose.project=gbrain", "pg_isready", "127.0.0.1 5433",
                     "launchctl print gui/$U/$l", "http://127.0.0.1:8199/health", "http://127.0.0.1:18085/health"):
        assert readback in rb, readback
    resume = rb.index("hermes cron resume 9b1a4fdaaa2a")
    last = rb[resume:]
    assert "hermes cron list" in last and all(i in last.split("hermes cron list", 1)[1]
                                               for i in ("9b1a4fdaaa2a", "a3bcde3fa92e", "7d0b9686f64e"))


# ---- t_d3252f19 fix 4: the watch pages fallback use, DeepInfra 401/402 and a dead tunnel, once per episode -------
def _req(via: str) -> str:
    return f"[rerank-proxy] req 10:00:00 caller=? ua=- docs=5 top_n=3 q_len=4 q_sent=4 q=abcd1234 via={via} code=200 ms=9\n"


@pytest.fixture()
def watch(tmp_path, monkeypatch):
    """gbrain-stack-watch with a host file in tmp, a fake docker (sh) and fake /health (get_json)."""
    mod = load("gbrain_stack_watch", STACK / "gbrain-stack-watch")
    (tmp_path / "hosts").mkdir()
    rd = tmp_path / "receipts"
    (tmp_path / "hosts" / "t.toml").write_text(
        f'compose_project = "gbrain"\nrevision = "{"a" * 40}"\ngbrain_home = "{tmp_path}"\n'
        f'receipts_dir = "{rd}"\nrerank_tunnel = true\nports = {{ serve = 1, rerank = 2 }}\n')
    monkeypatch.setattr(mod, "HERE", tmp_path)
    st = {"down": set(), "log": "", "health": {"status": "ok", "primary_ok": True, "fallback_armed": True}}

    def sh(*cmd, timeout=30, merge=False):
        if cmd[:2] == ("docker", "ps") and "-q" in cmd:
            svc = cmd[-1].rsplit("=", 1)[1]
            return 0, "" if svc in st["down"] else f"cid-{svc}"
        if cmd[:2] == ("docker", "inspect"):
            return 0, "a" * 40 + "|[]"
        if cmd[:2] == ("docker", "logs"):
            return 0, st["log"]
        return 0, ""

    def get_json(url, timeout=8):
        return {"engine": "postgres", "status": "ok"} if url.endswith(":1/health") else st["health"]

    monkeypatch.setattr(mod, "sh", sh)
    monkeypatch.setattr(mod, "get_json", get_json)
    return mod, st, rd


def _run_watch(mod, capsys):
    rc = mod.main(["--host", "t", "--no-github"])
    return rc, capsys.readouterr().out


def test_watch_green_is_silent(watch, capsys):
    mod, st, _ = watch
    st["log"] = _req("primary") * 50 + _req("fallback")       # 1 of 51: below the threshold
    assert _run_watch(mod, capsys) == (0, "")


def test_watch_checks_the_tunnel_when_the_profile_is_on(watch, capsys):
    mod, st, _ = watch
    st["down"].add("rerank-tunnel")
    rc, out = _run_watch(mod, capsys)
    assert rc == 1 and "service rerank-tunnel NOT RUNNING" in out


@pytest.mark.parametrize("log,expect", [
    (_req("primary") * 10 + _req("fallback") * 3, "3 of 13 requests"),
    (_req("none") + "[rerank-proxy] fallback ALSO failed: HTTP 402\n", "HTTP 402: OUT OF FUNDS"),
    ("[rerank-proxy] fallback ALSO failed: HTTP 401\n", "HTTP 401: rejected the key"),
])
def test_watch_pages_fallback_use_and_deepinfra_auth(watch, capsys, log, expect):
    mod, st, _ = watch
    st["log"] = log
    rc, out = _run_watch(mod, capsys)
    assert rc == 1 and expect in out, out


def test_watch_pages_unarmed_fallback(watch, capsys):
    mod, st, _ = watch
    st["health"] = {"status": "ok", "primary_ok": True, "fallback_armed": False}
    rc, out = _run_watch(mod, capsys)
    assert rc == 1 and "fallback NOT armed" in out


def test_watch_pages_each_failure_once(watch, capsys):
    mod, st, rd = watch
    rd.mkdir()
    (rd / "ARMED-AT").write_text("")
    for job in mod.PERIOD_GRACE_S:
        (rd / f"{job}.json").write_text(json.dumps({"result": "ok"}))
    fail = {"result": "fail", "rc": 1, "ended": "2026-11-21T03:30:00", "tail": ["boom"]}
    (rd / "pg-backup.json").write_text(json.dumps(fail))
    st["down"].add("rerank-tunnel")
    rc, out = _run_watch(mod, capsys)
    assert rc == 1 and "cron job pg-backup fail" in out and "rerank-tunnel NOT RUNNING" in out
    assert _run_watch(mod, capsys) == (0, "")                  # the same failures an hour later: no repeat
    (rd / "pg-backup.json").write_text(json.dumps({**fail, "ended": "2026-11-22T03:30:00"}))
    rc, out = _run_watch(mod, capsys)                          # a new failed run pages, the open tunnel does not
    assert rc == 1 and "2026-11-22T03:30:00" in out and "rerank-tunnel" not in out
    st["down"].clear()
    assert _run_watch(mod, capsys) == (0, "")                  # tunnel back: the episode closes
    st["down"].add("rerank-tunnel")
    rc, out = _run_watch(mod, capsys)                          # and a new episode pages again
    assert rc == 1 and "rerank-tunnel NOT RUNNING" in out


def test_watch_check_reports_every_finding_and_keeps_no_state(watch, capsys):
    """--check (cutover C14, Prism 5fd839303bc5): the same finding twice is rc 1 twice, and no episode file."""
    mod, st, rd = watch
    st["down"].add("rerank-tunnel")
    for _ in range(2):
        assert mod.main(["--host", "t", "--no-github", "--check"]) == 1
        assert "rerank-tunnel NOT RUNNING" in capsys.readouterr().out
    assert not (rd / mod.STATE_FILE).exists()


def test_watch_state_write_does_not_follow_a_planted_symlink(watch, tmp_path):
    """Prism 110df17353ff: the receipts dir is container-writable, so a symlink planted at a predictable temp path
    must not be written through."""
    mod, _, rd = watch
    rd.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    state = rd / mod.STATE_FILE
    (rd / (mod.STATE_FILE + ".tmp")).symlink_to(victim)
    mod.page_new([("k", "t")], state)
    assert victim.read_text() == "keep\n"
    assert json.loads(state.read_text())["open"] == ["k"] and not state.is_symlink()
    assert sorted(p.name for p in rd.iterdir()) == sorted([mod.STATE_FILE, mod.STATE_FILE + ".tmp"])


# ---- t_d3252f19 fix 5: pg-backup fails a dump below 80% of the last good run's processed bytes ------------------
FAKE_RESTIC = """#!/bin/sh
d=$(dirname "$0")
echo "$*" >> "$d/restic.log"
case "$*" in
  *backup*) printf '{"message_type":"summary","snapshot_id":"abcdef0123456789","total_bytes_processed":%s,"data_added":5}\\n' "$(cat "$d/proc")" ;;
esac
exit 0
"""


def _backup(tmp_path, proc: int, prev: int | None):
    bindir, stamps, st = tmp_path / "bin", tmp_path / "stamps", tmp_path / "state"
    for d in (bindir, stamps, st):
        d.mkdir(exist_ok=True)
    (bindir / "restic").write_text(FAKE_RESTIC)
    (bindir / "restic").chmod(0o755)
    (bindir / "proc").write_text(str(proc))
    (tmp_path / "pw").write_text("pw")
    stamp = stamps / "last-success-gbrain-pg-backup"
    if prev is not None:
        stamp.write_text(f"1700000000 snapshot=00000000 processed={prev} added=1\n")
    elif stamp.exists():
        stamp.unlink()
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "GBRAIN_STATE_DIR": str(st),
           "GBRAIN_RESTIC_REPO": "x", "GBRAIN_RESTIC_HOST": "h", "RESTIC_PASSWORD_FILE": str(tmp_path / "pw"),
           "GBRAIN_BACKUP_STAMP_DIR": str(stamps)}
    r = subprocess.run(["bash", str(STACK / "cron" / "jobs" / "pg-backup.sh")], env=env, capture_output=True,
                       text=True, timeout=60)
    return r, stamp.read_text() if stamp.exists() else None, (bindir / "restic.log").read_text()


@pytest.mark.parametrize("proc,prev,ok", [
    (7_900_000, 10_000_000, False),    # below 80% of the last good run
    (8_000_000, 10_000_000, True),     # exactly 80%
    (2_000_000, None, True),           # first run: the 1 MB floor only
    (900_000, None, False),            # first run below the floor
])
def test_pg_backup_size_check_against_last_good_run(tmp_path, proc, prev, ok):
    r, stamp, rlog = _backup(tmp_path, proc, prev)
    if ok:
        assert r.returncode == 0 and f"processed={proc} " in stamp, r.stdout + r.stderr
    else:
        assert r.returncode == 1 and "FAILED" in r.stdout, r.stdout + r.stderr
        assert stamp == (None if prev is None else f"1700000000 snapshot=00000000 processed={prev} added=1\n")
        assert "forget --retry-lock 15m abcdef0123456789" in rlog   # the short snapshot was dropped
