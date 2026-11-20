# t_0930e064 measurements (ACE-AI GPU0, 2026-11-19 16:32-17:31 PST)

Setup: test instances only (transient user units). Reranker = live `zerank_server.py` (md5 5930c7c6...)
on ports 18094/18095/18096, same weights, same GPU0. Requests = the 50 real rerank requests captured by
t_bdc77bdc (`/private/tmp/t_bdc7/requests50.json` on the Studio, md5 744d9f53...; 50 docs each), replayed
sequentially. ComfyUI = second instance of `~/ComfyUI` (v0.46.0) on port 18188, load = the image stage of
Ace's animegen workflow: anima-aesthetic-v1.1 + qwen_3_06b + qwen_image_vae, 1280x720, batch 4, 30 steps
er_sde/simple, cfg 4, seeds 20261119/20261120 alternating, jobs back to back. Paired phases alternate the
conditions per request. Live services were not touched; live zerank traffic (61 requests in the window)
still hit GPU0, so the right-hand columns drop every call that overlapped a live rerank (live call taken
as the 6 s before its journal completion line). The sibling card's bench was not running in any phase.

## D-1 / D-2: rerank latency (ms, 50 requests)

| phase | ComfyUI | mechanism | p50 | p90 | p90 vs idle | | n clean | p50 | p90 | p90 vs idle |
|---|---|---|---|---|---|---|---|---|---|---|
| A | idle | none | 4523 | 5682 | | | 44 | 4523 | 5333 | |
| A | idle | reranker as MPS client | 4530 | 5577 | -2% | | 44 | 4594 | 5321 | 0% |
| B | job running | none | 9329 | 11552 | +103% | | 42 | 9551 | 11552 | +117% |
| C | job running | MPS, ComfyUI client priority 1 (below normal) | 6143 | 7461 | +31% | | 46 | 6143 | 7103 | +33% |
| D | job running | MPS, priority 1 + ComfyUI active threads 50% | 5191 | 6144 | +8% | | 47 | 5191 | 6092 | +14% |
| B | job running | gate (paired with "none") | 5129 | 6568 | +16% | | 41 | 4919 | 5995 | +12% |
| F | job running | gate only, 5 s between reranks | 4714 | 7810 | +37% | | 36 | 4699 | 5754 | +8% |

Top-3 identical to the idle run's top-3 in every paired comparison (50/50).

## A-2 verdict: not met on the raw numbers

The bar is gate p90 within +25% of the ComfyUI-idle p90 (5682 ms raw, so at most 7103 ms). Run B passes
raw (6568 ms, +16%). Run F misses raw (7810 ms, +37%). Both pass only after dropping the calls that overlapped
a live rerank (+12%, +8% against 5333 ms clean). That filter is a post-hoc choice, so A-2 counts as a miss in
one of two runs. Phase F was not re-measured in a window without live rerank traffic.

## ComfyUI cost (wall s per job, same workflow; first job after a model load excluded)

| condition | job walls | output vs alone |
|---|---|---|
| alone (E) | 22.9, 23.2 | reference |
| MPS prio 1, reranks back to back (C) | 78.5, 82.5, 72.6 | identical (pixel sha256) |
| MPS prio 1 + 50% threads, reranks back to back (D) | 87.8, 133.9, 64.1 | DIFFERENT pixels |
| gate, half the reranks gated, back to back (B) | 62.4-72.5 (11 jobs) | identical |
| gate only, rerank ~5 s then 5 s gap (F) | 31.4, 44.6, 68.3, 43.5, 52.1, ... | identical |

The gate costs ComfyUI the rerank's duration and nothing else: in B, 50 pauses totalled 235 s (4.7 s each,
gate counter in the node). Both instances under MPS also work on driver 595.99.02 (ComfyUI dynamic VRAM
included), but MPS (C) leaves +31-33% on the rerank p90, and limiting ComfyUI's threads (D) changes its
output and made its jobs 3-6x slower than alone (with reranks running; D alone was not measured).

## (c) driver time-slice: not measured

`nvidia-smi compute-policy --set-timeslice` (GPU0 is at `Default`) is root-only and GPU-wide: it changes the
slice length for every context on the card, live services included, and gives no per-process priority
(contexts still round-robin), so it cannot give the reranker more than an equal share. Not tried.

## Raw data

`pair.jsonl` (per call: condition, request id, ms, top-3, GPU0 util before), `comfy.jsonl` (per job: wall,
seed, pixel hashes), live rerank completion timestamps: attached to card t_0930e064.
