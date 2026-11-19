"""Unit tests for zerank_server.py batching (t_bdc77bdc). No GPU, no model: torch, fastapi, pydantic and
sentence_transformers are stubbed before import. Run: python3 -m unittest deploy/zerank/test_zerank_server.py
"""
import importlib.util
import os
import random
import sys
import threading
import types
import unittest


def _load_server():
    torch = types.ModuleType("torch")
    torch.float16, torch.bfloat16, torch.float32 = "fp16", "bf16", "fp32"

    class _OOM(Exception):
        pass

    torch.cuda = types.SimpleNamespace(
        OutOfMemoryError=_OOM, reset_peak_memory_stats=lambda: None, empty_cache=lambda: None,
        max_memory_reserved=lambda: 0)
    fastapi = types.ModuleType("fastapi")

    class FastAPI:
        def get(self, *_a, **_k):
            return lambda f: f
        post = get

    fastapi.FastAPI = FastAPI
    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = object
    st = types.ModuleType("sentence_transformers")

    class CrossEncoder:
        def __init__(self, *_a, **_k):
            pass

        def parameters(self):
            yield types.SimpleNamespace(dtype="bf16")

    st.CrossEncoder = CrossEncoder
    sys.modules.update(torch=torch, fastapi=fastapi, pydantic=pydantic, sentence_transformers=st)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zerank_server.py")
    spec = importlib.util.spec_from_file_location("zerank_server_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S = _load_server()


class PredictSortedTest(unittest.TestCase):
    def test_scores_come_back_in_input_order(self):
        rng = random.Random(7)
        pairs = [("q", "x" * rng.randint(0, 500)) for _ in range(53)]
        calls = []

        def predict(batch):
            calls.append([len(p[1]) for p in batch])
            return [len(p[1]) * 1.5 for p in batch]  # score = a function of the pair itself

        out = S.predict_sorted(predict, pairs, 4, lambda p: len(p[1]))
        self.assertEqual(out, [len(p[1]) * 1.5 for p in pairs])
        # batches really were formed longest-first, at most batch_size each
        flat = [n for b in calls for n in b]
        self.assertEqual(flat, sorted(flat, reverse=True))
        self.assertTrue(all(len(b) <= 4 for b in calls))
        self.assertEqual(len(flat), len(pairs))

    def test_ties_and_single_batch(self):
        pairs = [("q", "a"), ("q", "bb"), ("q", "c"), ("q", "")]
        out = S.predict_sorted(lambda b: [ord((p[1] or "z")[0]) for p in b], pairs, 64, lambda p: len(p[1]))
        self.assertEqual(out, [ord("a"), ord("b"), ord("c"), ord("z")])

    def test_empty(self):
        self.assertEqual(S.predict_sorted(lambda b: [], [], 4, len), [])


class MergerTest(unittest.TestCase):
    def test_overlapping_requests_get_their_own_scores(self):
        gate = threading.Event()
        passes = []

        def score(pairs):
            passes.append(len(pairs))
            if len(passes) == 1:
                gate.wait(5)  # hold the first pass so the next requests queue up and merge
            return [float(p[1]) for p in pairs]

        m = S._Merger(score)
        reqs = [[("q", i * 100 + j) for j in range(n)] for i, n in enumerate([3, 5, 2, 4])]
        out = {}

        def run(k):
            out[k] = m.submit(reqs[k])

        first = threading.Thread(target=run, args=(0,))
        first.start()
        while not passes:
            pass
        rest = [threading.Thread(target=run, args=(k,)) for k in (1, 2, 3)]
        for t in rest:
            t.start()
        while sum(len(j[0]) for j in m._pending) < 11:
            pass
        gate.set()
        for t in [first] + rest:
            t.join(5)
        for k, r in enumerate(reqs):
            self.assertEqual(out[k], [float(p[1]) for p in r])
        self.assertEqual(passes, [3, 11])  # requests 1-3 shared one pass

    def test_failure_reaches_every_waiter(self):
        def score(pairs):
            raise RuntimeError("boom")

        m = S._Merger(score)
        with self.assertRaises(RuntimeError):
            m.submit([("q", "d")])


if __name__ == "__main__":
    unittest.main()
