"""Tests for the GPU0 yield gate (t_0930e064). Stdlib only:
    python3 -m unittest discover -s deploy/gpu-yield/tests -v
"""
import asyncio
import importlib
import os
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "comfyui_gpu_yield"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "zerank"))

import gate as gate_mod  # noqa: E402


def touch(d, name, age_s=0.0):
    p = os.path.join(d, name)
    with open(p, "w"):
        pass
    if age_s:
        t = time.time() - age_s
        os.utime(p, (t, t))
    return p


class GateTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="gpu-yield-test-")

    def gate(self, **kw):
        kw.setdefault("poll_s", 0.005)
        return gate_mod.Gate(self.d, **kw)

    def test_no_flag_not_busy(self):
        g = self.gate()
        self.assertEqual(g.refresh(), [])
        self.assertFalse(g.busy)

    def test_other_files_ignored(self):
        touch(self.d, "something-else")
        g = self.gate()
        g.refresh()
        self.assertFalse(g.busy)

    def test_waits_until_flag_clears(self):
        p = touch(self.d, "rerank-1-0")
        g = self.gate(max_pause_s=5)
        g.refresh()
        self.assertTrue(g.busy)
        import threading
        threading.Timer(0.3, os.unlink, args=(p,)).start()
        waited = g.wait()
        self.assertGreaterEqual(waited, 0.25)
        self.assertLess(waited, 2.0)
        self.assertEqual(g.timeouts, 0)
        self.assertFalse(g.busy)

    def test_stuck_flag_times_out_and_logs(self):
        """D-4/A-3: a flag nobody removes pauses at most max_pause_s, then the job runs for resume_s."""
        touch(self.d, "rerank-1-0")
        g = self.gate(max_pause_s=0.3, resume_s=0.5)
        g.refresh()
        with self.assertLogs("gpu_yield", level="WARNING") as cm:
            t = time.monotonic()
            waited = g.wait()
            elapsed = time.monotonic() - t
        self.assertGreaterEqual(waited, 0.3)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(g.timeouts, 1)
        self.assertIn("cap", cm.output[0])
        # inside the resume window the gate does not pause again, even though the flag is still there
        self.assertTrue(g.busy)
        self.assertEqual(g.wait(), 0.0)
        # after the window it pauses (and caps) again
        time.sleep(0.55)
        with self.assertLogs("gpu_yield", level="WARNING"):
            self.assertGreaterEqual(g.wait(), 0.3)
        self.assertEqual(g.timeouts, 2)

    def test_stale_flag_ignored_and_logged_once(self):
        touch(self.d, "rerank-9-9", age_s=120)
        g = self.gate(stale_s=60)
        with self.assertLogs("gpu_yield", level="WARNING") as cm:
            self.assertEqual(g.refresh(), [])
        self.assertFalse(g.busy)
        self.assertIn("stale", cm.output[0])
        with self.assertNoLogs("gpu_yield", level="WARNING"):
            g.refresh()

    def test_missing_dir_not_busy(self):
        g = gate_mod.Gate(os.path.join(self.d, "nope"))
        self.assertEqual(g.refresh(), [])
        self.assertFalse(g.busy)

    def test_poller_thread_tracks_flag(self):
        g = self.gate()
        g.start()
        try:
            p = touch(self.d, "rerank-1-0")
            time.sleep(0.1)
            self.assertTrue(g.busy)
            os.unlink(p)
            time.sleep(0.1)
            self.assertFalse(g.busy)
        finally:
            g.stop()

    def test_from_env_off_switch(self):
        self.assertIsNone(gate_mod.from_env({}))
        self.assertIsNone(gate_mod.from_env({"GPU_YIELD_DIR": "  "}))
        g = gate_mod.from_env({"GPU_YIELD_DIR": self.d, "GPU_YIELD_MAX_PAUSE_S": "7"})
        self.assertEqual(g.max_pause_s, 7.0)
        self.assertEqual(g.stale_s, 60.0)


class ZerankYieldTest(unittest.TestCase):
    """The reranker-side wrapper holds one flag file for the life of each POST /v1/rerank."""

    def load(self, ydir, inner):
        fake = types.ModuleType("zerank_server")
        fake.app = inner
        sys.modules["zerank_server"] = fake
        os.environ["GPU_YIELD_DIR"] = ydir
        try:
            sys.modules.pop("zerank_yield", None)
            return importlib.import_module("zerank_yield")
        finally:
            os.environ.pop("GPU_YIELD_DIR", None)

    def test_flag_held_during_request_and_removed_after(self):
        d = tempfile.mkdtemp()
        seen = []

        async def inner(scope, receive, send):
            seen.append(sorted(os.listdir(d)))
            if scope.get("boom"):
                raise RuntimeError("boom")

        m = self.load(d, inner)
        scope = {"type": "http", "path": "/v1/rerank", "method": "POST"}
        asyncio.run(m.app(scope, None, None))
        self.assertEqual(len(seen[0]), 1)
        self.assertTrue(seen[0][0].startswith("rerank-"))
        self.assertEqual(os.listdir(d), [])
        with self.assertRaises(RuntimeError):
            asyncio.run(m.app(dict(scope, boom=True), None, None))
        self.assertEqual(os.listdir(d), [])
        asyncio.run(m.app({"type": "http", "path": "/health", "method": "GET"}, None, None))
        self.assertEqual(seen[-1], [])  # no flag for non-rerank paths

    def test_off_when_dir_unset(self):
        seen = []

        async def inner(scope, receive, send):
            seen.append(scope["path"])

        fake = types.ModuleType("zerank_server")
        fake.app = inner
        sys.modules["zerank_server"] = fake
        os.environ.pop("GPU_YIELD_DIR", None)
        sys.modules.pop("zerank_yield", None)
        m = importlib.import_module("zerank_yield")
        self.assertEqual(m.YIELD_DIR, "")
        asyncio.run(m.app({"type": "http", "path": "/v1/rerank", "method": "POST"}, None, None))
        self.assertEqual(seen, ["/v1/rerank"])


if __name__ == "__main__":
    unittest.main()
