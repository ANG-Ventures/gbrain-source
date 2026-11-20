"""GPU0 priority flag for the zerank reranker (t_0930e064). Off unless GPU_YIELD_DIR is set.

ASGI wrapper around zerank_server:app. For the whole life of each POST /v1/rerank (queue wait behind the
server's lock included) it keeps one file in $GPU_YIELD_DIR. The ComfyUI node `comfyui_gpu_yield` pauses
sampling between ops while that directory holds a fresh file, so the rerank gets GPU0 to itself.

Run it instead of zerank_server:app:  uvicorn zerank_yield:app --app-dir <dir holding both files>
With GPU_YIELD_DIR unset the wrapper is a pass-through (no files, no extra syscalls).
"""
import itertools
import os

from zerank_server import app as _inner

YIELD_DIR = os.environ.get("GPU_YIELD_DIR", "").strip()
_seq = itertools.count()

if YIELD_DIR:
    os.makedirs(YIELD_DIR, mode=0o700, exist_ok=True)
    # A previous process of ours that died mid-request left files named <pid>-*; ComfyUI also ignores
    # any file older than its stale bound, this just stops the restart from leaving them for that long.
    for _n in os.listdir(YIELD_DIR):
        if _n.startswith("rerank-"):
            try:
                os.unlink(os.path.join(YIELD_DIR, _n))
            except OSError:
                pass


def _flag_path():
    return os.path.join(YIELD_DIR, f"rerank-{os.getpid()}-{next(_seq)}")


async def app(scope, receive, send):
    if not YIELD_DIR or scope.get("type") != "http" or scope.get("path") != "/v1/rerank" \
            or scope.get("method") != "POST":
        await _inner(scope, receive, send)
        return
    path = _flag_path()
    try:
        with open(path, "w"):
            pass
    except OSError:
        path = None  # never fail a rerank because the flag could not be written
    try:
        await _inner(scope, receive, send)
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
