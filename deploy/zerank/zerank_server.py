#!/usr/bin/env python3
"""zerank-2 native reranker server — ZE/llama-server-compatible /v1/rerank.

Serves zeroentropy/zerank-2-reranker (native bf16 HF weights via
sentence-transformers CrossEncoder) on the RTX PRO 6000. Wire shape matches
gbrain's llama-server-reranker recipe: POST /v1/rerank
{model, query, documents[]} -> {results: [{index, relevance_score}]}.
"""
import os
import threading

import torch

from fastapi import FastAPI
from pydantic import BaseModel
from sentence_transformers import CrossEncoder

MODEL_ID = os.environ.get("ZERANK_MODEL", "zeroentropy/zerank-2-reranker")
DEVICE = os.environ.get("ZERANK_DEVICE", "cuda")

model = CrossEncoder(MODEL_ID, device=DEVICE, trust_remote_code=True)

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
_PEAK_LOG_MB = 9500


@app.post("/v1/rerank")
def rerank(req: RerankReq):
    query = req.query[:_MAX_QUERY_CHARS] if _MAX_QUERY_CHARS > 0 else req.query
    pairs = [(query, (d or "")[:_MAX_DOC_CHARS]) for d in req.documents]
    with lock:
        torch.cuda.reset_peak_memory_stats()
        try:
            scores = model.predict(pairs, batch_size=4)
        except torch.cuda.OutOfMemoryError:
            # A failed batch leaves its tensors ALLOCATED (the exception path
            # skips predict-internal cleanup) — reclaim before retrying, and
            # retry at batch_size=1 (smallest activation peak). Without the
            # gc+empty_cache, every OOM permanently ratchets process VRAM
            # (observed live 2026-08-15: 7.7GB -> 10.8GB after one OOM).
            import gc
            gc.collect()
            torch.cuda.empty_cache()
            scores = model.predict(pairs, batch_size=1)
        finally:
            import gc
            gc.collect()
            torch.cuda.empty_cache()
            peak_mb = torch.cuda.max_memory_reserved() // 2**20
            if peak_mb > _PEAK_LOG_MB:
                print(
                    f"[peak] request peaked at {peak_mb}MB reserved: "
                    f"{len(req.documents)} docs, largest "
                    f"{max((len(d or chr(32)) for d in req.documents), default=0)} chars",
                    flush=True,
                )
    results = [
        {"index": i, "relevance_score": float(s)} for i, s in enumerate(scores)
    ]
    results.sort(key=lambda r: -r["relevance_score"])
    if req.top_n:
        results = results[: req.top_n]
    return {"model": req.model or MODEL_ID, "object": "list", "results": results}
