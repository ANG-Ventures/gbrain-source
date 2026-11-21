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
