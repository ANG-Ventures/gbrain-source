# Search stage timing (fork-local)

Every MCP `search` and `query` call served by `gbrain serve --http` writes a
per-stage breakdown into `mcp_request_log.stage_timing` (JSONB, migration
v147), next to the existing `latency_ms`. The code is
`src/core/search/stage-timing.ts`.

## Row shape

A real row from `test/search-stage-timing.serial.test.ts` (PGLite, mocked
embedder, reranker delayed 300 ms; latency 359.4 ms). Rows written by the
serve also carry the `pre_dispatch` stage and `dispatch_ms`, `auth_ms` and
`inflight`.

```json
{"v":1,"stages":{"op_config":0.6,"config":14.4,"cache_embed":0.3,"cache_lookup":3.3,
 "intent":0.1,"keyword":7.8,"relational":0.5,"embed":0.1,"vector":4.7,"fusion":0.7,
 "rescore":2.1,"post_fusion":12.3,"rerank":301,"post_rerank":2.9,"format":0.1,
 "meta_hook":0},"unattributed_ms":8.4,"query_len":26,"rerank_in":6,
 "rerank_top_n_in":25,"rerank_out":6,"cache":"miss"}
```

- `stages` are exclusive wall-clock ms. Only stages that ran appear. A stage
  name that runs more than once in one call (`config`, `op_config`,
  `post_rerank`) is summed.
- `unattributed_ms` = `latency_ms` minus the sum of `stages`: the time no stage
  covers. A large value means the instrumentation has a gap, not that the time
  vanished.
- Outside `latency_ms` (informational, not in the sum): `auth_ms` is the
  bearer-token check that runs before the handler starts its clock;
  `dispatch_ms` is the inclusive dispatch time; `inflight` is the number of
  `/mcp` requests open when this one started.
- No query text is stored, only `query_len`.

| stage | what it covers |
|---|---|
| `pre_dispatch` | `/mcp` entry to dispatch: surface + writeback config, MCP SDK parse, federated-scope read |
| `op_config` | the op's own config reads (snippet cap, private-page gate, keyword-only flag) |
| `config` | search-mode, merged config, intent patterns, cache config reads (cached wrapper and inner search) |
| `cache_embed` | the query embedding used for the semantic-cache lookup |
| `cache_lookup` | the semantic-cache query |
| `intent` | query intent classification |
| `keyword` | keyword + title arms (run concurrently, one span) |
| `relational` | relational recall arm |
| `modality_llm`, `expansion` | LLM tie-break and query expansion (`query` op; off for `search`) |
| `embed` | the vector-arm query embedding(s) |
| `vector` | `searchVector` fan-out |
| `fusion` | unverified-stub stamping + RRF |
| `rescore` | cosine re-score (fetches chunk embeddings) |
| `post_fusion` | backlink / salience / recency / graph / alias / supersede boosts |
| `rerank` | the reranker call |
| `post_rerank` | alias hop, exact-lookup tier, content-flag stamp |
| `format` | JSON serialization of the result |
| `meta_hook` | `_meta.brain_hot_memory` hook |

Untimed paths (their time lands in `unattributed_ms`): the keyword-only
fallbacks taken when embedding is unavailable or fails, unified/image
cross-modal embeds, two-pass walks, CRAG escalation config and think.

## Reading it

```
scripts/search-stage-report.sh                 # last 24 hours, slow = 8000 ms
scripts/search-stage-report.sh "2 hours" 5000  # custom window and threshold
```

Prints p50/p90/p99/max per stage, then every request over the threshold with
the stage that held the most time. It uses `psql "$DATABASE_URL"` when set,
else `docker exec gbrain-pg psql -U gbrain -d gbrain`.

One-off SQL:

```sql
SELECT created_at, latency_ms, stage_timing->'stages' FROM mcp_request_log
WHERE operation = 'search' AND latency_ms > 8000 ORDER BY created_at DESC LIMIT 20;
```

## Turning it off

`GBRAIN_STAGE_TIMING=off` (or `0` / `false`) in the serve's environment, then
restart the serve. Rows then carry `stage_timing = NULL`. Timing is fail-open:
an error in the timing code drops the timing, never the search or the ledger
row (if the `stage_timing` insert fails, the row is written without it).

## Upstream sync

This is a fork-local seam. On the next upstream sync: keep
`stage-timing.ts` and its test as-is; re-apply the `timeStage(...)` wraps in
`hybrid.ts`, `ops/search.ts` and `mcp/dispatch.ts` and the `/mcp` hunks in
`serve-http.ts` (a conflict there means upstream moved the call); renumber
migration v147 above upstream's latest version (the ALTER is
`ADD COLUMN IF NOT EXISTS`, so re-running it under a new number is a no-op).

## Per-caller budget (t_b0ef077a)

A caller with a hard deadline can be given a search budget, so it gets the
un-reranked top-k instead of a timeout when a stage runs long:

```
gbrain config set search.caller_budget_ms '{"rail-prod":12000}'
```

Keys are OAuth client names (`mcp_request_log.agent_name`), values are ms from
the start of the `search` op. Read per call; no restart. With a budget, the
reranker call gets `min(provider timeout, time left - 250 ms)`; with under
300 ms left it is not called. Either way the results come back in RRF order and
`meta.degraded` carries `{stage: 'rerank_budget', reason: 'timeout' |
'budget_exhausted'}`; `stage_timing` carries `rerank_budget` (and
`rerank_budget_ms` when the call was capped). A budget timeout writes no
rerank-failure audit row and does not trip the reranker health alert. The
semantic cache never stores such a result: the budget is per caller, so a
stored row would be served un-reranked to callers with no budget.
Remove with `gbrain config unset search.caller_budget_ms`.

The lexical arms (chunk FTS + title FTS) see at most 2000 characters of the
query (`LEXICAL_QUERY_MAX_CHARS` in `hybrid.ts`): longer turns compiled to
1100-1900-node tsqueries that alone ran past the 8 s statement timeout.
Vector and rerank still see the full text.
