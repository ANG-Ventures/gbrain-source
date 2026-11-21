#!/usr/bin/env python3
"""gbrain rerank failover proxy — local zerank-2 first, DeepInfra Qwen3 fallback.

CONTAINER COPY (deploy/stack/rerank, card t_ab2fcba3) of ANG-Ventures/gbrain rerank-failover-proxy.py @335cb5d
(sha256 6435c994...). The logic is unchanged; only the host couplings read the environment so the same file runs
in the gbrain stack's `rerank-proxy` service:
  RERANK_PRIMARY_BASE  primary base URL (default http://127.0.0.1:18084; the stack points it at the tunnel service)
  RERANK_LISTEN        host:port to bind (default 127.0.0.1:18085; the stack binds 0.0.0.0:18085)
  DEEPINFRA_KEY_FILE   read the fallback key from this file (the stack's rendered /run/secrets/deepinfra-key)
                       instead of 1Password at startup; unset keeps the op-cached-read path
  RERANK_NOTIFY_CMD    alert command (default ~/.hermes/scripts/notify). The container has no fleet notifier: the
                       stack sets it to /bin/true and the host-side deploy/stack/bin/gbrain-stack-watch pages when
                       this proxy's /health says primary_ok=false (the fallback is engaged)

Listens on 127.0.0.1:18085, speaking the llama-server/ZE /v1/rerank wire shape
gbrain's llama-server-reranker recipe expects.

Primary:  http://127.0.0.1:18084/v1/rerank  (SSH tunnel -> ACE-AI zerank-native)
Fallback: DeepInfra Qwen/Qwen3-Reranker-4B  (cloud, per-token) — engaged only on
          primary connection error / timeout / 5xx.

DeepInfra key is read at startup from 1Password (via op-cached-read); if unavailable the
proxy still runs primary-only (fallback disabled, logged once).

NOTE: score SCALES differ between the two backends (zerank raw logits vs Qwen3
probabilities). gbrain uses scores for ORDERING within a single response, so
mixed scales across requests are acceptable for a rarely-engaged fallback.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PRIMARY_BASE = os.environ.get("RERANK_PRIMARY_BASE", "http://127.0.0.1:18084").rstrip("/")
PRIMARY = f"{PRIMARY_BASE}/v1/rerank"
PRIMARY_TIMEOUT_S = 25
# Measured 2026-11-09 (126 reranks, 40-50 docs each): query <1k chars -> p50 3.5 s; 5-10k -> 13 s;
# >10k -> 19 s, and the 25 s primary timeout trips above ~13k. gbrain caps the query at 500 chars
# for its own expansion step but hands the reranker the whole user turn (pasted logs included), so
# the cross-encoder scores 50 pairs of (turn + doc). Cap it here; 80% of queries are under 2k.
MAX_QUERY_CHARS = int(os.environ.get("RERANK_MAX_QUERY_CHARS", "2000"))
FALLBACK_MODEL = "Qwen/Qwen3-Reranker-4B"
FALLBACK_URL = f"https://api.deepinfra.com/v1/inference/{FALLBACK_MODEL}"
FALLBACK_TIMEOUT_S = 30


def _origin(url: str) -> tuple[str, str]:
    p = urllib.parse.urlsplit(url)
    return p.scheme, (p.hostname or "")


# The only places this proxy may send a request: the configured primary (RERANK_PRIMARY_BASE, http/https only) and
# the DeepInfra fallback. A RERANK_PRIMARY_BASE with another scheme (file://, ftp://, ...) stops the proxy at start.
if _origin(PRIMARY_BASE)[0] not in ("http", "https") or not _origin(PRIMARY_BASE)[1]:
    sys.exit(f"[rerank-proxy] RERANK_PRIMARY_BASE must be an http(s) URL with a host: {PRIMARY_BASE!r}")
ALLOWED_ORIGINS = {_origin(PRIMARY_BASE), _origin(FALLBACK_URL)}


def _checked_url(url: str) -> str:
    """url, if its scheme is http/https and its (scheme, host) is the primary's or the fallback's; else ValueError."""
    o = _origin(url)
    if o[0] not in ("http", "https") or o not in ALLOWED_ORIGINS:
        raise ValueError(f"refusing request to {o[0]}://{o[1]}: not the configured primary or the fallback")
    return url
OP_REF = "op://p5fyqekdzsdp7rmdk3n4kjecse/DeepInfra/credential"

def _load_key() -> str | None:
    key_file = os.environ.get("DEEPINFRA_KEY_FILE")
    if key_file:
        try:
            with open(key_file) as f:
                return f.read().strip() or None
        except OSError as e:
            print(f"[rerank-proxy] key file unreadable: {e}", flush=True)
            return None
    tok_path = os.path.expanduser("~/.hermes/.op-service-token")
    try:
        with open(tok_path) as f:
            tok = f.read().strip()
        env = {**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": tok, "OP_CACHE": "false"}
        # 2026-09-09: op-cached-read first (serves last-known-good on a 1P 5xx/429 at
        # service restart -> reranker keeps its key); raw op stays as the fallback.
        ocr = os.path.expanduser("~/.hermes/scripts/op-cached-read")
        cmd = [ocr, OP_REF] if os.access(ocr, os.X_OK) else ["/opt/homebrew/bin/op", "--cache=false", "read", OP_REF]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=45, env=env)
        key = out.stdout.strip()
        return key or None
    except Exception as e:
        print(f"[rerank-proxy] op read failed: {e}", flush=True)
        return None

DEEPINFRA_KEY = _load_key()
if not DEEPINFRA_KEY:
    print("[rerank-proxy] WARNING: no DeepInfra key — fallback DISABLED", flush=True)

_ALERT_COOLDOWN_S = 6 * 3600  # once per 6h per alert kind
_last_alert: dict[str, float] = {}

def _alert_once(kind: str, severity: str, body: str) -> None:
    """Fire a fleet alert, rate-limited per kind. Never raises."""
    now = time.time()
    if now - _last_alert.get(kind, 0) < _ALERT_COOLDOWN_S:
        return
    _last_alert[kind] = now
    try:
        subprocess.run(
            [os.path.expanduser(os.environ.get("RERANK_NOTIFY_CMD", "~/.hermes/scripts/notify")),
             "--severity", severity, "--source", "gbrain-rerank-proxy",
             "--body", body],
            timeout=30, capture_output=True,
        )
        print(f"[rerank-proxy] ALERT sent: {kind}", flush=True)
    except Exception as e:
        print(f"[rerank-proxy] alert send failed ({kind}): {e}", flush=True)

def _post(url: str, body: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        _checked_url(url), data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
    )
    # url passed _checked_url above: http(s) and the configured primary or the DeepInfra fallback only
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        return json.load(r)

def _caller(port: int) -> str:
    """Name the local process on the other end of a loopback connection (pid + command), or '?'."""
    try:
        out = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP@127.0.0.1:{port}", "-sTCP:ESTABLISHED", "-Fpc"],
            capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return "?"
    pid = cmd = None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("c") and pid and pid != str(os.getpid()):
            cmd = line[1:]
            break
    return f"{cmd}[{pid}]" if cmd else "?"


def _access(caller: str, ua: str, query: str, n_docs: int, top_n, via: str, code: int, t0: float,
            q_raw_len: int = 0) -> None:
    """One line per rerank request (Ace 2026-11-09): who called, how big, how slow, which backend.
    The query text is never logged, only its length and a short hash."""
    try:
        import hashlib
        qh = hashlib.sha256(query.encode("utf-8", "replace")).hexdigest()[:8]
        print(f"[rerank-proxy] req {time.strftime('%H:%M:%S')} caller={caller} ua={ua or '-'} "
              f"docs={n_docs} top_n={top_n} q_len={q_raw_len or len(query)} q_sent={len(query)} q={qh} via={via} code={code} "
              f"ms={(time.monotonic() - t0) * 1000:.0f}", flush=True)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # quiet default access log
        pass

    def _respond(self, code: int, payload: dict):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            primary_ok = True
            try:
                health = _checked_url(f"{PRIMARY_BASE}/health")   # http(s), the configured primary only
                urllib.request.urlopen(health, timeout=4)  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            except Exception:
                primary_ok = False
            self._respond(200, {
                "status": "ok",
                "primary_ok": primary_ok,
                "fallback_armed": bool(DEEPINFRA_KEY),
            })
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/rerank"):
            self._respond(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._respond(400, {"error": "bad request"})
            return

        query = body.get("query", "")
        q_raw_len = len(query)
        if MAX_QUERY_CHARS > 0 and q_raw_len > MAX_QUERY_CHARS:
            query = query[:MAX_QUERY_CHARS]
            body["query"] = query
        documents = body.get("documents", [])
        top_n = body.get("top_n")
        t0 = time.monotonic()
        port = self.client_address[1]
        ua = self.headers.get("User-Agent", "")
        who: list = []
        finder = threading.Thread(target=lambda: who.append(_caller(port)), daemon=True)
        finder.start()  # resolve the caller while the connection is still open, off the request path

        def log(via, code):
            finder.join(3)
            _access(who[0] if who else "?", ua, query, len(documents), top_n, via, code, t0, q_raw_len)

        # --- primary: local zerank-2 over the tunnel ---
        try:
            out = _post(PRIMARY, body, {}, PRIMARY_TIMEOUT_S)
            self._respond(200, out)
            log("primary", 200)
            return
        except Exception as e:
            print(f"[rerank-proxy] primary failed ({e}); "
                  f"fallback={'armed' if DEEPINFRA_KEY else 'DISABLED'}", flush=True)

        # --- fallback: DeepInfra Qwen3-Reranker-4B ---
        if not DEEPINFRA_KEY:
            self._respond(502, {"error": "primary down, fallback disabled"})
            log("none", 502)
            return
        try:
            di = _post(
                FALLBACK_URL,
                {"queries": [query] * len(documents), "documents": documents},
                {"Authorization": f"bearer {DEEPINFRA_KEY}"},
                FALLBACK_TIMEOUT_S,
            )
            scores = di.get("scores", [])
            results = [
                {"index": i, "relevance_score": float(s)}
                for i, s in enumerate(scores)
            ]
            results.sort(key=lambda r: -r["relevance_score"])
            if top_n:
                results = results[:top_n]
            self._respond(200, {
                "model": f"deepinfra:{FALLBACK_MODEL}",
                "object": "list",
                "results": results,
            })
            print(f"[rerank-proxy] served via FALLBACK ({len(documents)} docs)", flush=True)
            log("fallback", 200)
            _alert_once(
                "fallback_engaged", "medium",
                "gbrain rerank running on DeepInfra FALLBACK — local zerank-2 "
                "(ACE-AI tunnel :18084) is unreachable. Searches still reranked "
                "(cloud, ~$0.11/day at full traffic). Check: zerank-native.service "
                "on ACE-AI + ai.gbrain.rerank-tunnel LaunchAgent.",
            )
        except urllib.error.HTTPError as e:
            if e.code == 402:
                _alert_once(
                    "deepinfra_no_funds", "high",
                    "gbrain rerank fallback OUT OF FUNDS: DeepInfra returned 402 "
                    "Payment Required AND the local zerank-2 primary is down — "
                    "searches are degrading to un-reranked RRF order. "
                    "Top up: https://deepinfra.com/dash/billing",
                )
            print(f"[rerank-proxy] fallback ALSO failed: HTTP {e.code}", flush=True)
            self._respond(502, {"error": "both rerank backends failed"})
            log("none", 502)
        except Exception as e:
            print(f"[rerank-proxy] fallback ALSO failed: {e}", flush=True)
            self._respond(502, {"error": "both rerank backends failed"})
            log("none", 502)

if __name__ == "__main__":
    host, _, port = os.environ.get("RERANK_LISTEN", "127.0.0.1:18085").rpartition(":")
    srv = ThreadingHTTPServer((host, int(port)), Handler)
    print(f"[rerank-proxy] listening on {host}:{port} primary={PRIMARY}", flush=True)
    srv.serve_forever()
