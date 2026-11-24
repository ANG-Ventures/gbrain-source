#!/usr/bin/env python3
"""standby/drill/drill_load.py: the callers of the gbrain swap drill (card t_dd1e1edb, STANDBY.md).

Two loops until <rig>/load.stop exists, one JSON line per attempt to <rig>/load.jsonl:
  search  every 0.5 s: MCP `search` on the CALLERS' loopback serve port (drill-a's, as on the Studio), as the drill
          OAuth client; ok = an answer with hits. This is what the mem0 prefetch and rail.sh see during a swap.
  write   every 0.5 s: one row into swapdrill_acks on whichever half accepts a write (psql over each half's loopback pg
          port); ok = the id came back (acked). After the swap, `summary` checks every acked id on the new primary.
`drill_load.py summary <rig> <new primary pg container>` prints the error windows and the acked-write check.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import http.client
import urllib.parse
from pathlib import Path


def env_file(p: Path) -> dict:
    out = {}
    for line in p.read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def log(f, rec):
    f.write(json.dumps(rec) + "\n")
    f.flush()


def hits(text: str) -> int:
    """The hit count of a search answer, parsed the way gbrain_swap.v_prove reads it."""
    try:
        h = json.loads(text)
        return len(h if isinstance(h, list) else h.get("results", []))
    except (ValueError, AttributeError):
        return len([ln for ln in text.splitlines() if ln.strip()])


def post(port: int, path: str, body: bytes, headers: dict, timeout: float) -> str:
    """POST to 127.0.0.1:<port><path>: loopback only, scheme and host fixed here (semgrep dynamic-urllib-use).
    An HTTP error status raises, as urlopen did."""
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    try:
        conn.request("POST", path, body=body, headers=headers)
        r = conn.getresponse()
        text = r.read().decode()
        if r.status >= 400:
            raise OSError(f"HTTP {r.status}")
        return text
    finally:
        conn.close()

def searcher(rig: Path, port: int, f, stop):
    c = env_file(rig / "prove-client.env")
    tok, exp = None, 0
    while not stop.is_set():
        t0 = time.time()
        rec = {"t": round(t0, 3), "kind": "search"}
        try:
            if not tok or time.time() > exp:
                form = urllib.parse.urlencode({"grant_type": "client_credentials",
                                               "client_id": c["GBRAIN_RAIL_CLIENT_ID"],
                                               "client_secret": c["GBRAIN_RAIL_CLIENT_SECRET"]}).encode()
                d = json.loads(post(port, "/token", form, {"Content-Type": "application/x-www-form-urlencoded"}, 5))
                tok, exp = d["access_token"], time.time() + int(d.get("expires_in", 3600)) - 60
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "search", "arguments": {"query": "standby swap drill", "limit": 3}}})
            raw = post(port, "/mcp", body.encode(), {"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
                                                     "Accept": "application/json, text/event-stream"}, 20)
            data = "".join(ln[5:].lstrip() for ln in raw.splitlines() if ln.startswith("data:")) or raw
            d = json.loads(data)
            text = ((d.get("result") or {}).get("content") or [{}])[0].get("text", "")
            rec["hits"] = hits(text)
            # ok = an answer WITH hits: `[]` is a caller that still has nothing (Prism 252e2a694d24)
            rec["ok"] = rec["hits"] > 0 and "error" not in d and not (d.get("result") or {}).get("isError")
        except Exception as e:  # noqa: BLE001 - every failure is a data point
            rec["ok"], rec["err"] = False, f"{type(e).__name__}: {str(e)[:80]}"
            tok = None if "401" in str(e) else tok
        rec["ms"] = round((time.time() - t0) * 1000)
        log(f, rec)
        time.sleep(max(0.0, 0.5 - (time.time() - t0)))


def writer(halves: list[str], f, stop):
    n = 0
    while not stop.is_set():
        t0 = time.time()
        n += 1
        rec = {"t": round(t0, 3), "kind": "write", "seq": n, "ok": False}
        for c in halves:
            try:
                r = subprocess.run(["docker", "exec", c, "psql", "-X", "-At", "-U", "gbrain", "-d", "gbrain", "-c",
                                    f"insert into swapdrill_acks(seq) values ({n}) returning id"],
                                   capture_output=True, text=True, timeout=10)
            except subprocess.TimeoutExpired:   # a failed attempt, not the end of the writer (Prism a7a800cba8e7)
                rec.setdefault("errs", []).append(f"{c}: timeout 10s")
                continue
            if r.returncode == 0 and r.stdout.strip().splitlines()[:1] and r.stdout.strip().splitlines()[0].isdigit():
                rec.update(ok=True, via=c, id=int(r.stdout.strip().splitlines()[0]))
                break
            rec.setdefault("errs", []).append(f"{c}: {(r.stderr or r.stdout).strip()[-60:]}")
        rec["ms"] = round((time.time() - t0) * 1000)
        log(f, rec)
        time.sleep(max(0.0, 0.5 - (time.time() - t0)))


def windows(recs: list[dict]) -> dict:
    """The longest run of consecutive failures (first failure -> first success after it), and the total failed."""
    longest, cur0, fails = 0.0, None, 0
    for r in recs:
        if not r["ok"]:
            fails += 1
            cur0 = r["t"] if cur0 is None else cur0
        elif cur0 is not None:
            longest = max(longest, r["t"] - cur0)
            cur0 = None
    if cur0 is not None:
        longest = max(longest, recs[-1]["t"] - cur0)
    return {"attempts": len(recs), "failed": fails, "longest_error_window_s": round(longest, 1)}


def summary(rig: Path, newp: str) -> int:
    recs = [json.loads(ln) for ln in (rig / "load.jsonl").read_text().splitlines() if ln.strip()]
    out = {k: windows([r for r in recs if r["kind"] == k]) for k in ("search", "write")}
    acked = sorted(r["id"] for r in recs if r["kind"] == "write" and r["ok"])
    q = subprocess.run(["docker", "exec", newp, "psql", "-X", "-At", "-U", "gbrain", "-d", "gbrain", "-c",
                        "select id from swapdrill_acks"], capture_output=True, text=True, timeout=30)
    have = {int(x) for x in q.stdout.split() if x.isdigit()}
    missing = [i for i in acked if i not in have]
    out["acked_writes"] = {"acked": len(acked), "present_on_new_primary": len(acked) - len(missing),
                           "missing": missing[:20]}
    # no evidence is not a pass: a failed read of the new primary, or not one acked write (Prism e4391a7df3ec)
    if q.returncode:
        out["acked_writes"]["error"] = f"query rc={q.returncode}: {q.stderr.strip()[-120:]}"
    print(json.dumps(out))
    return 0 if (not missing and acked and q.returncode == 0) else 1


def main() -> int:
    if sys.argv[1] == "summary":
        return summary(Path(sys.argv[2]), sys.argv[3])
    rig, port, halves = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3].split(",")
    stop = threading.Event()
    with open(rig / "load.jsonl", "a") as f:
        ts = [threading.Thread(target=searcher, args=(rig, port, f, stop), daemon=True),
              threading.Thread(target=writer, args=(halves, f, stop), daemon=True)]
        for t in ts:
            t.start()
        while not (rig / "load.stop").exists():
            time.sleep(0.5)
        stop.set()
        for t in ts:
            t.join(timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
