# zerank reranker server (ACE-AI)

The reranker behind gbrain search. It runs on ACE-AI GPU0 as user `ace`, from
`~/zerank-serve/zerank_server.py` under the user unit `zerank-native.service` (port 8084,
loopback). The files here are the versioned copies:

| repo file | live path on ACE-AI |
|---|---|
| `zerank_server.py` | `~/zerank-serve/zerank_server.py` |
| `zerank-native.service` | `~/.config/systemd/user/zerank-native.service` |
| `zerank-native.service.d/50-growth-cap.conf` | `~/.config/systemd/user/zerank-native.service.d/50-growth-cap.conf` |

Commit ca512418 holds the live files byte-for-byte as of 2026-11-19 (server md5
5930c7c67a61720e89ca8df591b32125). Later commits add the knobs below.

## Knobs (environment)

| variable | default | old value | what it does |
|---|---|---|---|
| `ZERANK_EMPTY_CACHE` | `0` | `1` | `1` runs gc + `torch.cuda.empty_cache()` after every request. `0` keeps the allocator cache: reserved VRAM stays at the largest pass seen (7.7 GB idle with `1`; up to 10.4 GB over the 50 bench requests with `0`). The OOM path still frees it. Scores are bit-identical either way. |
| `ZERANK_BATCH_SIZE` | `4` | `4` | pairs per forward pass |
| `ZERANK_DTYPE` | `auto` | `auto` | `auto` = the weights' dtype (bf16); `fp16`/`bf16`/`fp32` force one |
| `ZERANK_SORT` | `chars` | `chars` | `chars` = sentence-transformers' own length sort; `tokens` = sort by token count, restore order |
| `ZERANK_MERGE` | `0` | `0` | `1` = requests that queue behind a running pass are scored together in the next pass |
| `ZERANK_MAX_QUERY_CHARS` | `2000` | `2000` | query cut |
| `ZERANK_PEAK_LOG_MB` | `9500` | `9500` | log a `[peak]` line when one pass reserves more VRAM |

Any change of batch size, sort order or merging changes which pairs share a forward pass. In
bf16 that moves scores by about one bf16 step and flips the top-3 of a few requests in 100
(measurements: card t_bdc77bdc). Only `ZERANK_EMPTY_CACHE` leaves the scores bit-identical.

Rollback to the old behaviour without changing the file: set `ZERANK_EMPTY_CACHE=1` (all other
defaults are the old values) in a drop-in and restart the unit.

## Tests

    python3 -m unittest deploy/zerank/test_zerank_server.py

No GPU or model needed: torch, fastapi, pydantic and sentence-transformers are stubbed.
