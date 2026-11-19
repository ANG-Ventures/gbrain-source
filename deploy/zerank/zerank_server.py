#!/usr/bin/env python3
"""zerank-2 native reranker server — ZE/llama-server-compatible /v1/rerank.
Serves zeroentropy/zerank-2-reranker (native bf16 HF weights via
sentence-transformers CrossEncoder) on the RTX PRO 6000. Wire shape matches
gbrain's llama-server-reranker recipe: POST /v1/rerank
{model, query, documents[]} -> {results: [{index, relevance_score}]}.

Speed knobs (t_bdc77bdc). Rollback to the pre-t_bdc77bdc behaviour:
  ZERANK_BATCH_SIZE=4 ZERANK_DTYPE=auto ZERANK_EMPTY_CACHE=1 ZERANK_SORT=chars ZERANK_MERGE=0
"""
import os
import threading
import torch
from fastapi import FastAPI
from pydantic import BaseModel
from sentence_transformers import CrossEncoder
MODEL_ID = os.environ.get("ZERANK_MODEL", "zeroentropy/zerank-2-reranker")
DEVICE = os.environ.get("ZERANK_DEVICE", "cuda")
# Pairs per forward pass. Was 4.
_BATCH_SIZE = int(os.environ.get("ZERANK_BATCH_SIZE", "4"))
# auto = the weights' own dtype (bf16 for zerank-2); fp16 / bf16 / fp32 force one.
_DTYPE = os.environ.get("ZERANK_DTYPE", "auto")
# 1 = gc.collect() + torch.cuda.empty_cache() after every request (the old behaviour).
# 0 = keep the allocator cache between requests; the OOM path still reclaims.
_EMPTY_CACHE = os.environ.get("ZERANK_EMPTY_CACHE", "1") == "1"
# chars = sentence-transformers' own length sort (by characters) inside predict().
# tokens = sort the pairs by token count first, batch in that order, restore input order.
_SORT = os.environ.get("ZERANK_SORT", "chars")
# 1 = requests that arrive while a batch is running are scored together in the next pass
# (one predict over all of them) instead of queueing one by one behind the lock.
_MERGE = os.environ.get("ZERANK_MERGE", "0") == "1"
_DTYPES = {"fp16": torch.float16, "bf16": torch.bfloat16, "fp32": torch.float32}
_model_kwargs = {"dtype": _DTYPES[_DTYPE]} if _DTYPE in _DTYPES else {}
model = CrossEncoder(MODEL_ID, device=DEVICE, trust_remote_code=True, model_kwargs=_model_kwargs)
print(
    f"[zerank] batch_size={_BATCH_SIZE} dtype={next(model.parameters()).dtype} "
    f"empty_cache={int(_EMPTY_CACHE)} sort={_SORT} merge={int(_MERGE)}",
    flush=True,
)
lock = threading.Lock()
app = FastAPI()
class RerankReq(BaseModel):
    model: str | None = None
    query: str
    documents: list[str]
    top_n: int | None = None
@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_ID}
@app.get("/memstats")
def memstats():
    import torch
    return {
        "allocated_mb": round(torch.cuda.memory_allocated() / 2**20),
        "reserved_mb": round(torch.cuda.memory_reserved() / 2**20),
        "max_allocated_mb": round(torch.cuda.max_memory_allocated() / 2**20),
        "max_reserved_mb": round(torch.cuda.max_memory_reserved() / 2**20),
        "num_alloc_retries": torch.cuda.memory_stats().get("num_alloc_retries", 0),
    }
_MAX_DOC_CHARS = 20000  # bf16 activations for 8x(>3k-token) docs OOM a shared 24GB card
# Latency scales with QUERY length (measured 2026-11-09, 50 docs: <1k chars 3.5 s, 5-10k 13 s, >10k 19 s,
# peak 15 GB vs 9.5). Callers hand over whole user turns (pasted logs); the Studio proxy caps at 2000 too.
_MAX_QUERY_CHARS = int(os.environ.get("ZERANK_MAX_QUERY_CHARS", "2000"))
# Log a journald line whenever a single request pushes reserved VRAM past this
# (journald persists across restarts -> peak HISTORY with request ATTRIBUTION,
# which is what the 2026-08-15 creep hunt lacked). Normal 30-doc bursts peak
# ~8.6GB; the OOM-class starts ~11GB+.
_PEAK_LOG_MB = int(os.environ.get("ZERANK_PEAK_LOG_MB", "9500"))


def predict_sorted(predict, pairs, batch_size, length):
    """Score pairs in batches of similar length, return scores in INPUT order.

    predict(batch) -> scores for that batch, in batch order. length(pair) -> sort key.
    """
    order = sorted(range(len(pairs)), key=lambda i: -length(pairs[i]))
    scores = [0.0] * len(pairs)
    for start in range(0, len(order), batch_size):
        idx = order[start : start + batch_size]
        for i, s in zip(idx, predict([pairs[i] for i in idx])):
            scores[i] = float(s)
    return scores


def _token_length(pair):
    return len(model.tokenizer(pair[0], pair[1])["input_ids"])


def _predict(pairs, batch_size):
    if _SORT == "tokens":
        return predict_sorted(
            lambda b: model.predict(b, batch_size=len(b)), pairs, batch_size, _token_length
        )
    return [float(s) for s in model.predict(pairs, batch_size=batch_size)]


def _score(pairs):
    """One scoring pass over pairs under the lock (OOM retry + optional cache release)."""
    with lock:
        torch.cuda.reset_peak_memory_stats()
        try:
            return _predict(pairs, _BATCH_SIZE)
        except torch.cuda.OutOfMemoryError:
            # A failed batch leaves its tensors ALLOCATED (the exception path
            # skips predict-internal cleanup) — reclaim before retrying, and
            # retry at batch_size=1 (smallest activation peak). Without the
            # gc+empty_cache, every OOM permanently ratchets process VRAM
            # (observed live 2026-08-15: 7.7GB -> 10.8GB after one OOM).
            import gc
            gc.collect()
            torch.cuda.empty_cache()
            return _predict(pairs, 1)
        finally:
            if _EMPTY_CACHE:
                import gc
                gc.collect()
                torch.cuda.empty_cache()
            peak_mb = torch.cuda.max_memory_reserved() // 2**20
            if peak_mb > _PEAK_LOG_MB:
                print(
                    f"[peak] pass peaked at {peak_mb}MB reserved: {len(pairs)} pairs, largest "
                    f"{max((len(p[1]) for p in pairs), default=0)} chars",
                    flush=True,
                )


class _Merger:
    """One worker thread scores everything queued since its last pass in a single pass, so
    requests that arrive while a pass runs share the next one instead of queueing one by one."""

    def __init__(self, score):
        self._score = score
        self._cv = threading.Condition()
        self._pending = []  # (pairs, done-event, result dict)
        threading.Thread(target=self._run, daemon=True, name="zerank-merge").start()

    def submit(self, pairs):
        job = (pairs, threading.Event(), {})
        with self._cv:
            self._pending.append(job)
            self._cv.notify()
        job[1].wait()
        if "err" in job[2]:
            raise job[2]["err"]
        return job[2]["scores"]

    def _run(self):
        while True:
            with self._cv:
                while not self._pending:
                    self._cv.wait()
                jobs, self._pending = self._pending, []
            try:
                scores = self._score([p for j in jobs for p in j[0]])
                k = 0
                for j in jobs:
                    j[2]["scores"] = scores[k : k + len(j[0])]
                    k += len(j[0])
            except BaseException as e:  # every waiter gets the failure
                for j in jobs:
                    j[2]["err"] = e
            for j in jobs:
                j[1].set()


_merger = _Merger(_score) if _MERGE else None


@app.post("/v1/rerank")
def rerank(req: RerankReq):
    query = req.query[:_MAX_QUERY_CHARS] if _MAX_QUERY_CHARS > 0 else req.query
    pairs = [(query, (d or "")[:_MAX_DOC_CHARS]) for d in req.documents]
    scores = _merger.submit(pairs) if _MERGE else _score(pairs)
    results = [
        {"index": i, "relevance_score": float(s)} for i, s in enumerate(scores)
    ]
    results.sort(key=lambda r: -r["relevance_score"])
    if req.top_n:
        results = results[: req.top_n]
    return {"model": req.model or MODEL_ID, "object": "list", "results": results}
