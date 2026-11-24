"""deploy/stack/tests/test_standby.py: the gbrain warm standby + swap (card t_dd1e1edb, STANDBY.md).

  python3 -m pytest deploy/stack/tests -q

Pins the parts a later edit could silently break: which compose files a host's ROLE selects, the doctor's verdict
(the line the watch and the operator read), the link the primary-side script accepts, the standby's pin parity with
the primary, and the swap refusals that keep two primaries from ever running.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

STACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STACK))
import gbrain_standby as SB  # noqa: E402
import gbrain_swap as SW  # noqa: E402

G = SB.G
PE = STACK / "standby" / "primary-enable.sh"


def host(name: str) -> dict:
    return SB.load(name, None)


# ---------------------------------------------------------------- compose files by ROLE

def test_compose_files_follow_role(tmp_path):
    nas, studio = host("nas-fleet"), host("studio")
    assert G.compose_files(nas, tmp_path)[-1] == G.STANDBY_OVERLAY          # no ROLE: the profile exactly
    assert G.STANDBY_OVERLAY not in G.compose_files(studio, tmp_path)
    (tmp_path / "ROLE").write_text("primary\n")
    assert G.STANDBY_OVERLAY not in G.compose_files(nas, tmp_path)          # promoted: the overlay leaves the set
    (tmp_path / "ROLE").write_text("standby\n")
    assert G.compose_files(studio, tmp_path)[-1] == G.STANDBY_OVERLAY       # demoted Studio: the overlay joins
    assert G.compose_files(nas, tmp_path).count(G.STANDBY_OVERLAY) == 1
    (tmp_path / "ROLE").write_text("garbage\n")
    assert G.compose_files(studio, tmp_path) == studio["compose_files"]     # an unknown ROLE is ignored, not guessed
    argv = G.compose_argv(nas, tmp_path)
    assert argv[argv.index("-p") + 1] == "gbrain"


def test_standby_overlay_keeps_server_and_cron_unstarted():
    t = (STACK / "docker-compose.standby.yaml").read_text()
    for svc in ("server", "cron"):
        assert re.search(rf"^  {svc}:\n    profiles: \[\"promoted\"\]", t, re.M), svc
    # the link publishes the serve port on loopback only (the flip), and is capped like every stack container
    assert '"127.0.0.1:${GBRAIN_SERVE_PORT:?GBRAIN_SERVE_PORT unset}:8199"' in t
    assert "cpus: 0.25" in t and "mem_limit: 64m" in t


# ---------------------------------------------------------------- profiles

def test_profiles_pair_and_pin_parity():
    nas, studio = host("nas-fleet"), host("studio")
    sn, ss = SB.standby_cfg(nas), SB.standby_cfg(studio)
    assert sn["primary"] == "studio" and ss["primary"] == "nas-fleet"
    assert nas["revision"] == studio["revision"]               # the doctor compares the created images with this
    short = studio["revision"][:12]
    for k in ("server_image", "rerank_image", "cron_image"):
        assert nas[k].endswith(f":{short}-amd64") and studio[k].endswith(f":{short}-arm64"), k
    assert sn["primary_pg"] == f"127.0.0.1:{studio['ports']['pg']}"
    assert sn["primary_serve"] == f"127.0.0.1:{studio['ports']['serve']}"
    assert ss["primary_pg"] == f"127.0.0.1:{nas['ports']['pg']}"
    assert sn["repl_role"] != ss["repl_role"] and sn["slot"] != ss["slot"]
    assert nas["swap"]["callers_host"] == studio["swap"]["callers_host"] == "studio"
    assert nas["link"]["keys_file"] and studio["link"]["keys_file"]
    assert nas["ports"] == studio["ports"]                      # a swap moves no caller port


def test_drill_profiles_never_name_production(tmp_path):
    prod = host("studio"), host("nas-fleet")
    for x in ("a", "b"):
        raw = (STACK / "standby" / "drill" / f"drill-{x}.toml").read_text()
        p = tmp_path / f"drill-{x}.toml"
        p.write_text(raw.replace("@RIG@", str(tmp_path)).replace("@HOSTKEY@", "ssh-ed25519 AAAA"))
        h = SB.load(None, str(p))
        sb = SB.standby_cfg(h)
        assert h["swap"]["drill"] is True and h["nas_backup"] is False
        for pr in prod:
            assert h["compose_project"] != pr["compose_project"] and h["pg_volume"] != pr["pg_volume"]
            assert h["pg_container"] != pr["pg_container"]
            assert not set(h["ports"].values()) & set(pr["ports"].values())
        assert sb["data_root"].startswith("/srv/gbrain-standby/drill-")


# ---------------------------------------------------------------- the link

def test_ssh_config_forwards_pg_and_serve_only():
    sb = SB.standby_cfg(host("nas-fleet"))
    t = SB.ssh_config_text(sb)
    fw = re.findall(r"LocalForward (\S+) (\S+)", t)
    assert fw == [("0.0.0.0:5432", "127.0.0.1:5433"), ("0.0.0.0:8199", "127.0.0.1:8199")]
    assert "StrictHostKeyChecking yes" in t and "IdentitiesOnly yes" in t and "HostName 192.168.1.18" in t


def test_key_line_is_the_shape_primary_enable_accepts():
    sb = SB.standby_cfg(host("nas-fleet"))
    line = SB.key_line(sb, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample gbrain-standby@nas-fleet\n", "192.168.1.154")
    assert line.startswith('restrict,port-forwarding,from="192.168.1.154",permitopen="127.0.0.1:5433",'
                           'permitopen="127.0.0.1:8199" ssh-ed25519 ')
    pat = re.search(r'\[\[ \$STANDBY_KEY_LINE =~ (.+) \]\] \\\n', PE.read_text()).group(1)
    r = subprocess.run(["bash", "-c", f'STANDBY_KEY_LINE=$1; [[ $STANDBY_KEY_LINE =~ {pat} ]]', "_", line])
    assert r.returncode == 0
    bad = line.replace('restrict,port-forwarding,', 'port-forwarding,')
    r = subprocess.run(["bash", "-c", f'STANDBY_KEY_LINE=$1; [[ $STANDBY_KEY_LINE =~ {pat} ]]', "_", bad])
    assert r.returncode == 1


def test_scram_verifier_shape_matches_primary_enable():
    v = SB.scram_verifier("pw", salt=b"0123456789abcdef")
    assert re.fullmatch(r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+", v)
    assert SB.scram_verifier("pw", salt=b"0123456789abcdef") == v


def test_primary_enable_parses_and_refuses_bad_inputs():
    assert subprocess.run(["bash", "-n", str(PE)]).returncode == 0
    r = subprocess.run(["bash", str(PE), "check"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "STANDBY_MARK": "# mem0-standby x"})
    assert r.returncode == 2 and "bad STANDBY_MARK" in r.stderr
    r = subprocess.run(["bash", str(PE), "check"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "STANDBY_SLOT": "Bad-Slot"})
    assert r.returncode == 2 and "bad role/slot" in r.stderr


# ---------------------------------------------------------------- the doctor

PIN = "7969d9685cfb37a2d7c695a6a94d4d4aaa4f4ae7"


def obs(**over) -> dict:
    o = {"containers": {"primary-link": {"running": True},
                        "server": {"running": False, "status": "created", "image": "s", "revision": PIN},
                        "cron": {"running": False, "status": "created", "image": "c", "revision": PIN}},
         "pg": {"in_recovery": True, "receiver": "streaming", "last_msg_age_s": 0.4, "replay_lsn": "7D/5BEC278",
                "replay_ts_age_s": 3.0, "replay_advancing": None},
         "primary": {"current_lsn": "7D/5BEC278", "replay_lag_s": 0.1, "sender_state": "streaming",
                     "slot_wal_status": "reserved"},
         "pin": PIN, "rerank": {"primary_ok": True}}
    o.update(over)
    return o


def test_verdict_fresh_and_eligible():
    v = SB.verdict(obs())
    assert v["last"] == "STANDBY=FRESH lag_s=0.0 ELIGIBLE=yes"
    assert any(ln.startswith("rerank    primary_ok=True") for ln in v["lines"])


def test_verdict_lag_over_60s_is_stale():
    o = obs(primary={**obs()["primary"], "current_lsn": "7D/6BEC278", "replay_lag_s": 75.0})
    v = SB.verdict(o)
    assert v["last"].startswith("STANDBY=STALE lag_s=75.0 ELIGIBLE=no reason=STALE: lag_s=75.0 > 60")


def test_verdict_stalled_replay_uses_last_replay_age():
    o = obs(pg={**obs()["pg"], "replay_advancing": False, "replay_ts_age_s": 400.0},
            primary={**obs()["primary"], "current_lsn": "7D/6BEC278", "replay_lag_s": 0.2})
    assert SB.verdict(o)["lag_s"] == 400.0


@pytest.mark.parametrize("svc", ["server", "cron"])
def test_verdict_running_writer_on_standby_is_not_eligible(svc):
    cs = json.loads(json.dumps(obs()["containers"]))
    cs[svc].update(running=True, status="running")
    v = SB.verdict(obs(containers=cs))
    assert "ELIGIBLE=no" in v["last"] and f"{svc} is RUNNING on the standby" in v["reason"]


def test_verdict_image_revision_must_equal_primary_pin():
    cs = json.loads(json.dumps(obs()["containers"]))
    cs["server"]["revision"] = "0" * 40
    v = SB.verdict(obs(containers=cs))
    assert v["reason"].startswith("server revision 000000000000 != primary pin 7969d9685cfb")


def test_verdict_missing_containers_say_seed():
    cs = json.loads(json.dumps(obs()["containers"]))
    cs["cron"] = None
    assert SB.verdict(obs(containers=cs))["reason"] == "cron container not created (run `standby seed`)"


def test_verdict_primary_unreachable_is_stale_but_eligible():
    o = obs(primary={"error": "could not connect to server: Connection refused", "unreachable": True},
            pg={**obs()["pg"], "receiver": "none"})
    v = SB.verdict(o)
    assert v["last"] == "STANDBY=STALE lag_s=unknown ELIGIBLE=yes note=primary-unreachable last_msg_age_s=0.4"


def test_verdict_primary_auth_failure_is_unknown_not_outage():
    o = obs(primary={"error": "password authentication failed", "unreachable": SB.unreachable(
        "FATAL: password authentication failed for user", 2)})
    v = SB.verdict(o)
    assert "ELIGIBLE=no" in v["last"] and "primary state unknown" in v["reason"]


def test_verdict_lost_slot_says_reseed():
    v = SB.verdict(obs(primary={**obs()["primary"], "slot_wal_status": "lost"}))
    assert v["reason"] == "replication slot lost on the primary: reseed"


def test_verdict_promoted_replica():
    v = SB.verdict(obs(pg={**obs()["pg"], "in_recovery": False}))
    assert v["reason"].startswith("replica is not in recovery")


def test_rerank_down_is_reported_never_a_refusal():
    v = SB.verdict(obs(rerank={"primary_ok": False}))
    assert v["eligible"] and any("primary_ok=False" in ln for ln in v["lines"])


# ---------------------------------------------------------------- the swap

def test_outage_swap_is_refused():
    r = subprocess.run([sys.executable, str(STACK / "gbrainctl"), "swap", "--to", "nas-fleet",
                        "--old-primary-is-down", "power"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GBRAIN_SWAP_STATE": "/nonexistent/x"})
    assert r.returncode == 3 and "no outage swap" in r.stdout


def test_load_trio_real_profiles():
    t, o, c = SW.load_trio("nas-fleet", None)
    assert (t.name, o.name, c.name) == ("nas-fleet", "studio", "studio")
    assert c is o                                                     # prove runs where the callers are
    t, o, c = SW.load_trio("studio", None)
    assert (t.name, o.name, c.name) == ("studio", "nas-fleet", "studio") and c is t
    assert SW.Side(host("studio")).python == "/opt/homebrew/bin/python3.14"


def test_fence_problems():
    sid = "20261124T120000Z-to-nas-fleet"
    ok = {"containers": {"server": {"running": False}, "cron": {"running": False}, "postgres": {"running": False}},
          "role_file": "standby", "fence": {"swap_id": sid}}
    assert SW.fence_problems(ok, sid) == []
    bad = json.loads(json.dumps(ok))
    bad["containers"]["cron"]["running"] = True
    bad["role_file"] = None
    bad["fence"] = {"swap_id": "20261124T110000Z-to-studio"}
    p = SW.fence_problems(bad, sid)
    assert p[0] == "runs cron" and "ROLE=None" in p[1] and "fenced by swap 20261124T110000Z-to-studio" in p[2]


def test_steps_and_flip_order():
    # the flip (old host's link) comes after the new primary serves, and before prove reads through it
    assert SW.STEPS == ["preflight", "fence", "catchup", "promote", "start", "flip", "prove", "reseed_old"]


def test_side_verbs_refuse_without_swap_id(tmp_path):
    a = type("A", (), {"swap_id": None})()
    with pytest.raises(SW.StepFail):
        SW.need_sid(a)
    a.swap_id = "x; rm -rf /"
    with pytest.raises(SW.StepFail):
        SW.need_sid(a)


# ---------------------------------------------------------------- Prism round 1 (t_dd1e1edb)

def _sh(d: Path, script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-c", script.replace("cd /d", f"cd {d}")], capture_output=True, text=True)


def _files(d: Path) -> list[str]:
    return sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())


def _cluster(d: Path, names, sub="") -> None:
    for n in names:
        p = d / sub / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(n)


def test_reseed_restore_never_deletes_an_old_cluster_that_did_not_move(tmp_path):
    # mkdir OLD fails (phase 1 never started): the old cluster stays, the staged copy goes
    _cluster(tmp_path, ["PG_VERSION", "base/1"])
    _cluster(tmp_path, ["PG_VERSION", "standby.signal"], SB.NEXT)
    (tmp_path / "blocker").write_text("")
    sw = SB.switch_script().replace(f"mkdir -m 700 {SB.OLD}", f"mkdir -m 700 blocker/{SB.OLD}")
    assert "SWITCHED" not in _sh(tmp_path, sw).stdout
    assert "RESTORED" in _sh(tmp_path, SB.restore_script()).stdout
    assert _files(tmp_path) == ["PG_VERSION", "base/1", "blocker"]


def test_reseed_restore_after_a_partial_phase_one(tmp_path):
    _cluster(tmp_path, ["PG_VERSION", "base/1"])
    _cluster(tmp_path, ["global/x"], SB.OLD)                  # moved before the stop, no MOVED marker
    _cluster(tmp_path, ["PG_VERSION", "standby.signal"], SB.NEXT)
    assert "RESTORED" in _sh(tmp_path, SB.restore_script()).stdout
    assert _files(tmp_path) == ["PG_VERSION", "base/1", "global/x"]


def test_reseed_switch_then_restore_round_trip(tmp_path):
    _cluster(tmp_path, ["PG_VERSION", "base/1", "global/x"])
    _cluster(tmp_path, ["PG_VERSION", "standby.signal", "base/2"], SB.NEXT)
    assert "SWITCHED" in _sh(tmp_path, SB.switch_script()).stdout
    assert "standby.signal" in _files(tmp_path)
    assert "RESTORED" in _sh(tmp_path, SB.restore_script()).stdout
    assert _files(tmp_path) == ["PG_VERSION", "base/1", "global/x"]


def test_standby_overlay_refuses_postgres_without_standby_signal():
    t = (STACK / "docker-compose.standby.yaml").read_text()
    m = re.search(r"^  postgres:\n(.*?)(?=^  \S)", t, re.M | re.S)
    assert m
    body = m.group(1)
    assert 'entrypoint: ["/bin/sh", "-c"]' in body
    assert "if [ ! -f /var/lib/postgresql/data/standby.signal ]; then" in body
    assert "exit 78; fi; exec docker-entrypoint.sh postgres" in body


class _FakeSide:
    def __init__(self, name, answers):
        self.name, self.answers, self.calls = name, answers, []

    def call(self, verb, *args, timeout=0):
        self.calls.append((verb, args))
        a = self.answers[verb]
        return a(args) if callable(a) else dict(a)


def test_promote_reproves_catchup_against_the_current_fence_checkpoint(tmp_path):
    sid = "20261124T160000Z-to-nas-fleet"
    old = _FakeSide("studio", {
        "_fence": {"checkpoint_lsn": "7E/100", "was_armed": True},
        "_status": {"containers": {}, "role_file": "standby", "fence": {"swap_id": sid}}})
    target = _FakeSide("nas-fleet", {
        "_catchup": lambda args: {"replay_lsn": args[1], "checkpoint_lsn": args[1]},
        "_promote": {"pg_in_recovery": False}})
    st = {"steps": {"fence": {}, "catchup": {}}, "fence": {"checkpoint_lsn": "7D/5BEC278", "was_armed": True}}
    sw = SW.Swap(sid, st, target, old, old, tmp_path)
    r = sw.s_promote()
    assert [c[0] for c in target.calls] == ["_catchup", "_promote"]
    assert target.calls[0][1][:2] == ("--checkpoint-lsn", "7E/100")      # the re-fenced checkpoint, not the saved one
    assert sw.st["fence"]["checkpoint_lsn"] == "7E/100" and r["checkpoint_lsn"] == "7E/100"


def test_drill_load_counts_hits_and_refuses_an_empty_summary(tmp_path):
    spec = importlib.util.spec_from_file_location("drill_load", STACK / "standby" / "drill" / "drill_load.py")
    dl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dl)
    assert dl.hits("[]") == 0 and dl.hits('[{"slug": "a"}]') == 1 and dl.hits('{"results": []}') == 0
    (tmp_path / "load.jsonl").write_text(json.dumps({"t": 1, "kind": "write", "seq": 1, "ok": False}) + "\n")
    assert dl.summary(tmp_path, "no-such-container-t_dd1e1edb") == 1     # no acked write is no evidence


def test_primary_enable_replaces_tagged_lines_and_drops_any_slot():
    t = PE.read_text()
    assert 'if [[ $cur != "$HBA" ]]; then' in t and 'if [[ $have != "$want" ]]; then' in t
    assert "address='$gw' and netmask='255.255.255.255'" in t
    assert "where slot_name = '$slot' and not active" not in t
