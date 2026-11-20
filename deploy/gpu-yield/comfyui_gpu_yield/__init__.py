"""ComfyUI custom node (no nodes): pause sampling while the zerank reranker is busy (t_0930e064).

Off unless GPU_YIELD_DIR is set in ComfyUI's environment. When on, it wraps comfy.ops.run_every_op,
which ComfyUI calls at the start of every layer op, so the job stops launching GPU work within one
op of a rerank starting and continues where it stopped when the rerank ends. Nothing is interrupted,
cancelled or re-run; output is unchanged. Bounds and logging: see gate.py.
"""
import logging

from .gate import from_env

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

log = logging.getLogger("gpu_yield")
GATE = from_env()

if GATE is None:
    log.info("gpu_yield: off (GPU_YIELD_DIR unset)")
else:
    import comfy.ops

    _orig = getattr(comfy.ops, "run_every_op", None)
    if _orig is None:
        log.warning("gpu_yield: comfy.ops.run_every_op not found in this ComfyUI; gate NOT installed")
        GATE = None
    else:
        _gate = GATE

        def run_every_op():
            if _gate.busy:
                _gate.wait()
            _orig()

        comfy.ops.run_every_op = run_every_op
        GATE.start()
        log.info("gpu_yield: on, dir=%s max_pause=%.0fs stale=%.0fs resume=%.0fs poll=%.3fs",
                 GATE.flag_dir, GATE.max_pause_s, GATE.stale_s, GATE.resume_s, GATE.poll_s)
