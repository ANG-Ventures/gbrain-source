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
    (stack / ".env").write_text("# RENDERED by gbrainctl abc schema=def from hosts/studio.toml; do not edit\n")
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
