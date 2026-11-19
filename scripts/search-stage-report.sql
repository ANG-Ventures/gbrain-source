-- Search/query per-stage timing report over mcp_request_log.stage_timing
-- (migration v147, src/core/search/stage-timing.ts). Run it through
-- scripts/search-stage-report.sh, which fills three double-underscore
-- placeholders: WINDOW (an interval, e.g. 24 hours), THRESHOLD_MS (latency_ms
-- cut for the slow-request list, default 8000) and OPERATIONS (quoted op list,
-- e.g. 'search','query').
-- Two queries; the wrapper splits them at the marker line below query 1.

-- 1. p50/p90/p99/max per stage. n = requests in which the stage ran.
--    (unattributed) is latency_ms minus every recorded stage; (latency_ms) is
--    the row total, for scale.
WITH r AS (
  SELECT operation, latency_ms, stage_timing AS t
  FROM mcp_request_log
  WHERE created_at > now() - interval '__WINDOW__'
    AND operation IN (__OPERATIONS__)
    AND stage_timing IS NOT NULL
), s AS (
  SELECT r.operation, e.key AS stage, e.value::numeric AS ms
  FROM r CROSS JOIN LATERAL jsonb_each_text(r.t -> 'stages') AS e
  UNION ALL
  SELECT operation, '(unattributed)', (t ->> 'unattributed_ms')::numeric FROM r
  UNION ALL
  SELECT operation, '(latency_ms)', latency_ms::numeric FROM r
)
SELECT operation,
       stage,
       count(*) AS n,
       round(percentile_cont(0.50) WITHIN GROUP (ORDER BY ms)::numeric, 1) AS p50,
       round(percentile_cont(0.90) WITHIN GROUP (ORDER BY ms)::numeric, 1) AS p90,
       round(percentile_cont(0.99) WITHIN GROUP (ORDER BY ms)::numeric, 1) AS p99,
       round(max(ms), 1) AS max
FROM s
GROUP BY operation, stage
ORDER BY operation, p90 DESC;

-- @@
-- 2. Every request at or over the threshold, newest first, with the stage
--    that held the most time (unattributed competes as a stage).
WITH r AS (
  SELECT created_at, agent_name, operation, latency_ms, stage_timing AS t
  FROM mcp_request_log
  WHERE created_at > now() - interval '__WINDOW__'
    AND operation IN (__OPERATIONS__)
    AND stage_timing IS NOT NULL
    AND latency_ms >= __THRESHOLD_MS__
)
SELECT r.created_at,
       r.agent_name,
       r.operation,
       r.latency_ms,
       top.stage AS top_stage,
       top.ms AS top_ms,
       r.t ->> 'unattributed_ms' AS unattributed_ms,
       r.t ->> 'cache' AS cache,
       r.t ->> 'inflight' AS inflight,
       r.t ->> 'auth_ms' AS auth_ms,
       r.t -> 'stages' AS stages
FROM r
CROSS JOIN LATERAL (
  SELECT x.stage, x.ms
  FROM (
    SELECT e.key AS stage, e.value::numeric AS ms FROM jsonb_each_text(r.t -> 'stages') AS e
    UNION ALL
    SELECT '(unattributed)', (r.t ->> 'unattributed_ms')::numeric
  ) AS x
  ORDER BY x.ms DESC
  LIMIT 1
) AS top
ORDER BY r.created_at DESC;
