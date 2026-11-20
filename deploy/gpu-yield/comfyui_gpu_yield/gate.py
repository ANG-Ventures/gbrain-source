"""Pause gate: block the calling thread while a higher-priority GPU user holds a flag file.

Pure stdlib, no ComfyUI imports, so it can be unit-tested on any host (tests/test_gate.py).

Contract with the flag writer (deploy/zerank/zerank_yield.py): while a rerank is in flight there is a
file named `rerank-*` in the flag directory; it is removed when the request ends.

Bounds (D-4, a stuck flag must not freeze ComfyUI):
  * one pause never lasts longer than max_pause_s; when it hits the cap the gate logs a WARNING and lets
    the job run for resume_s before it will pause again;
  * a flag file older than stale_s is ignored (WARNING once per file), so a writer that died mid-request
    stops pausing anyone after stale_s.
"""
import logging
import os
import threading
import time

log = logging.getLogger("gpu_yield")

PREFIX = "rerank-"


class Gate:
    def __init__(self, flag_dir, max_pause_s=30.0, stale_s=60.0, resume_s=2.0, poll_s=0.01,
                 clock=time.time, mono=time.monotonic, sleep=time.sleep):
        self.flag_dir = flag_dir
        self.max_pause_s = float(max_pause_s)
        self.stale_s = float(stale_s)
        self.resume_s = float(resume_s)
        self.poll_s = float(poll_s)
        self._clock, self._mono, self._sleep = clock, mono, sleep
        self.busy = False            # read on the hot path; written by refresh()
        self._resume_until = 0.0
        self._warned_stale = set()
        self.pauses = 0
        self.paused_s = 0.0
        self.timeouts = 0
        self._stop = threading.Event()
        self._thread = None

    # -- flag scan -------------------------------------------------------------------------------
    def refresh(self):
        """Rescan the flag dir; set self.busy. Returns the fresh flag names."""
        fresh = []
        now = self._clock()
        try:
            with os.scandir(self.flag_dir) as it:
                for e in it:
                    if not e.name.startswith(PREFIX):
                        continue
                    try:
                        age = now - e.stat().st_mtime
                    except OSError:
                        continue  # removed between listdir and stat: the rerank just ended
                    if age > self.stale_s:
                        if e.name not in self._warned_stale:
                            self._warned_stale.add(e.name)
                            log.warning("gpu_yield: ignoring stale flag %s (age %.0f s > %.0f s)",
                                        e.name, age, self.stale_s)
                        continue
                    fresh.append(e.name)
        except FileNotFoundError:
            pass
        self.busy = bool(fresh)
        return fresh

    def start(self):
        def loop():
            while not self._stop.is_set():
                try:
                    self.refresh()
                except Exception:  # never let the poller die silently and leave busy=True
                    self.busy = False
                    log.exception("gpu_yield: flag scan failed")
                self._stop.wait(self.poll_s)
        self._thread = threading.Thread(target=loop, name="gpu_yield-poll", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # -- hot path --------------------------------------------------------------------------------
    def wait(self):
        """Called from the sampling thread when self.busy is True. Returns when the flag clears or
        the pause hits max_pause_s. Returns the seconds spent waiting."""
        t0 = self._mono()
        if t0 < self._resume_until:
            return 0.0
        deadline = t0 + self.max_pause_s
        timed_out = False
        while self.busy:
            if self._mono() >= deadline:
                timed_out = True
                break
            self._sleep(self.poll_s)
            if self._thread is None:      # no poller (tests / manual use): scan inline
                self.refresh()
        waited = self._mono() - t0
        self.pauses += 1
        self.paused_s += waited
        if timed_out:
            self.timeouts += 1
            self._resume_until = self._mono() + self.resume_s
            log.warning("gpu_yield: pause hit the %.0f s cap with flags still set; running %.0f s before "
                        "pausing again (timeouts=%d)", self.max_pause_s, self.resume_s, self.timeouts)
        else:
            log.info("gpu_yield: paused %.2f s for a rerank (pauses=%d total %.1f s)",
                     waited, self.pauses, self.paused_s)
        return waited


def from_env(env=os.environ):
    """Gate from GPU_YIELD_* env, or None when GPU_YIELD_DIR is unset/empty (the off switch)."""
    d = env.get("GPU_YIELD_DIR", "").strip()
    if not d:
        return None
    return Gate(d,
                max_pause_s=float(env.get("GPU_YIELD_MAX_PAUSE_S", "30")),
                stale_s=float(env.get("GPU_YIELD_STALE_S", "60")),
                resume_s=float(env.get("GPU_YIELD_RESUME_S", "2")),
                poll_s=float(env.get("GPU_YIELD_POLL_S", "0.01")))
