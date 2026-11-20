"""Unit tests for zerank_server.py batching (t_bdc77bdc). No GPU, no model: torch, fastapi, pydantic and
sentence_transformers are stubbed before import. Run: python3 -m unittest deploy/zerank/test_zerank_server.py
"""
import importlib.util
import os
import random
import sys
import types
import unittest


def _load_server():
    torch = types.ModuleType("torch")

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


if __name__ == "__main__":
    unittest.main()
