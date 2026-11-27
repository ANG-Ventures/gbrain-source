/**
 * t_c05f77e3 — per-stage search timing (src/core/search/stage-timing.ts).
 *
 * A-1: a 300 ms delay injected into one stage at a time lands in that stage's
 *      field and in no other; stages + unattributed_ms reconcile with the
 *      measured latency.
 * A-2: a timing layer that throws (broken clock) never fails the search.
 * Kill switch, cache-hit shape, the ledger insert (with and without the v147
 * column), and the scripts/search-stage-report.sql queries on a real
 * Postgres (PGLite).
 *
 * Serial: mock.module (embedding + reranker) + process.env mutation.
 */

import { afterAll, beforeAll, describe, expect, mock, test } from 'bun:test';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import * as realEmbedding from '../src/core/embedding.ts';
import * as realRerank from '../src/core/search/rerank.ts';

/**
 * Injected clock for A-1: only the injected delays advance it, so the real work
 * between them (PGLite queries, config loads) costs 0 ms on it and a busy runner
 * cannot move time into an uninstrumented stage. t_7087f163: on a loaded CI
 * runner the `config` stage once took over 150 ms of wall time and failed the
 * old "every other stage under 150 ms" check.
 */
let virtualNow = 0;
const sleep = (ms: number) =>
  new Promise<void>((r) => setTimeout(() => { virtualNow += ms; r(); }, ms));
const DELAY = 300;

/** Per-stage injected delay; reset before each run. */
const delays: Record<string, number> = {};
let embedCall = 0;

/**
 * Deterministic text-seeded pseudo-random 1536d unit vector: distinct texts are
 * near-orthogonal, so distinct queries never share a semantic-cache row.
 */
function textEmbedding(text: string): Float32Array {
  let h = 2166136261;
  for (const ch of text) h = Math.imul(h ^ ch.charCodeAt(0), 16777619) >>> 0;
  const rand = () => {
    h = (h + 0x6d2b79f5) >>> 0;
    let t = h;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296 - 0.5;
  };
  const arr = new Float32Array(1536);
  let norm = 0;
  for (let i = 0; i < 1536; i++) {
    arr[i] = rand();
    norm += arr[i] * arr[i];
  }
  norm = Math.sqrt(norm);
  for (let i = 0; i < 1536; i++) arr[i] /= norm;
  return arr;
}

mock.module('../src/core/embedding.ts', () => ({
  ...realEmbedding,
  embed: async (texts: string[]) => texts.map(textEmbedding),
  embedQuery: async (text: string) => {
    embedCall += 1;
    // call 1 = the cache-lookup embed (hybridSearchCached), call 2 = the inner
    // vector-arm embed (hybridSearch).
    if (embedCall === 1 && delays.cache_embed) await sleep(delays.cache_embed);
    if (embedCall === 2 && delays.embed) await sleep(delays.embed);
    return textEmbedding(text);
  },
}));

mock.module('../src/core/search/rerank.ts', () => ({
  ...realRerank,
  applyReranker: async (_q: string, results: unknown[]) => {
    if (delays.rerank) await sleep(delays.rerank);
    return results;
  },
}));

const { dispatchToolCall } = await import('../src/mcp/dispatch.ts');
const { configureGateway, resetGateway } = await import('../src/core/ai/gateway.ts');
const { PGLiteEngine } = await import('../src/core/pglite-engine.ts');
const { SemanticQueryCache } = await import('../src/core/search/query-cache.ts');
const {
  createStageTimer,
  finalizeStageTiming,
  insertToolCallLog,
  runWithStageTimer,
  stageClock,
  _setStageTimingClockForTests,
} = await import('../src/core/search/stage-timing.ts');

let engine: InstanceType<typeof PGLiteEngine>;
let tmpHome: string;
const savedHome = process.env.GBRAIN_HOME;
const savedTiming = process.env.GBRAIN_STAGE_TIMING;
const restores: Array<() => void> = [];

/** Wrap an engine method with the injected delay for `stage`. */
function delayEngine(method: string, stage: string): void {
  const e = engine as unknown as Record<string, (...a: unknown[]) => Promise<unknown>>;
  const orig = e[method]!;
  e[method] = async (...a: unknown[]) => {
    if (delays[stage]) await sleep(delays[stage]);
    return orig.apply(engine, a);
  };
  restores.push(() => { e[method] = orig; });
}

beforeAll(async () => {
  tmpHome = mkdtempSync(join(tmpdir(), 'gbrain-stage-timing-'));
  process.env.GBRAIN_HOME = tmpHome;
  delete process.env.GBRAIN_STAGE_TIMING;
  resetGateway();
  configureGateway({
    embedding_model: 'openai:text-embedding-3-large',
    embedding_dimensions: 1536,
    env: { OPENAI_API_KEY: 'sk-fake' },
  });
  engine = new PGLiteEngine();
  await engine.connect({});
  await engine.initSchema();
  await engine.setConfig('search.reranker.enabled', 'true');

  const words = ['builder', 'latency', 'search', 'timing', 'stage'];
  for (let i = 0; i < 6; i++) {
    const slug = `notes/page-${i}`;
    const truth = `Page ${i} is about ${words.join(' ')} number ${i}.`;
    await engine.putPage(slug, { type: 'note', title: `Page ${i}`, compiled_truth: truth });
    await engine.upsertChunks(slug, [
      { chunk_index: 0, chunk_text: truth, chunk_source: 'compiled_truth', embedding: textEmbedding(truth) },
    ]);
  }

  delayEngine('searchKeyword', 'keyword');
  delayEngine('searchVector', 'vector');
  delayEngine('getBacklinkCounts', 'post_fusion');
  delayEngine('getEmbeddingsByChunkIds', 'rescore');
  delayEngine('getContentFlagsByPageIds', 'post_rerank');
  const origLookup = SemanticQueryCache.prototype.lookup;
  SemanticQueryCache.prototype.lookup = async function (this: unknown, ...a: unknown[]) {
    if (delays.cache_lookup) await sleep(delays.cache_lookup);
    return (origLookup as (...x: unknown[]) => Promise<unknown>).apply(this, a);
  } as typeof origLookup;
  restores.push(() => { SemanticQueryCache.prototype.lookup = origLookup; });
}, 120_000);

afterAll(async () => {
  for (const r of restores.reverse()) r();
  _setStageTimingClockForTests(null);
  if (savedHome === undefined) delete process.env.GBRAIN_HOME;
  else process.env.GBRAIN_HOME = savedHome;
  if (savedTiming === undefined) delete process.env.GBRAIN_STAGE_TIMING;
  else process.env.GBRAIN_STAGE_TIMING = savedTiming;
  try { await engine.disconnect(); } catch { /* ignore */ }
  resetGateway();
  try { rmSync(tmpHome, { recursive: true, force: true }); } catch { /* ignore */ }
});

let runN = 0;

/** One MCP `search` inside a stage timer, finalized like serve-http does. */
async function timedSearch(query: string) {
  embedCall = 0;
  const timer = createStageTimer();
  const metaHook = async () => {
    if (delays.meta_hook) await sleep(delays.meta_hook);
    return {};
  };
  const result = await runWithStageTimer(timer, () =>
    dispatchToolCall(engine, 'search', { query, limit: 5 }, {
      remote: true,
      transport: 'http',
      sourceId: 'default',
      metaHook,
    }),
  );
  const latency = timer ? stageClock() - timer.startedAt : 0;
  const timing = finalizeStageTiming(timer, latency) as
    | { stages: Record<string, number>; unattributed_ms: number; [k: string]: unknown }
    | null;
  const rows = JSON.parse(result.content[0]!.text) as unknown[];
  return { result, rows, timing, latency };
}

/** Stages this test can isolate with a delay (each wraps a distinct call). */
const ISOLATED = [
  'cache_embed',
  'cache_lookup',
  'keyword',
  'embed',
  'vector',
  'rescore',
  'post_fusion',
  'rerank',
  'post_rerank',
  'meta_hook',
] as const;

describe('A-1: an injected 300 ms delay lands in its own stage only', () => {
  beforeAll(() => _setStageTimingClockForTests(() => virtualNow));
  afterAll(() => _setStageTimingClockForTests(null));
  for (const stage of ISOLATED) {
    test(stage, async () => {
      for (const k of Object.keys(delays)) delete delays[k];
      delays[stage] = DELAY;
      runN += 1;
      const { rows, timing, latency } = await timedSearch(`builder latency ${stage} run ${runN}`);
      expect(rows.length).toBeGreaterThan(0);
      expect(timing).not.toBeNull();
      const t = timing!;
      // On the injected clock the whole delay lands in `stage` and nowhere else.
      expect(t.stages[stage]).toBe(DELAY);
      for (const [other, ms] of Object.entries(t.stages)) {
        if (other !== stage) expect({ stage: other, ms }).toEqual({ stage: other, ms: 0 });
      }
      // D-2: stages + unattributed reconcile with the measured latency.
      expect(latency).toBe(DELAY);
      const sum = Object.values(t.stages).reduce((a, b) => a + b, 0);
      expect(sum + t.unattributed_ms).toBe(latency);
      expect(t.unattributed_ms).toBe(0);
      expect(t.cache).toBe('miss');
      expect(t.query_len).toBe(`builder latency ${stage} run ${runN}`.length);
      expect(typeof t.rerank_in).toBe('number');
      expect(typeof t.rerank_out).toBe('number');
    }, 30_000);
  }
});

describe('cache hit', () => {
  test('a repeated query records cache=hit and no retrieval stages', async () => {
    for (const k of Object.keys(delays)) delete delays[k];
    const q = 'builder latency cache hit probe';
    await timedSearch(q);
    // cache.store is fire-and-forget; give it a moment to land.
    const { awaitPendingSearchCacheWrites } = await import('../src/core/search/hybrid.ts');
    await awaitPendingSearchCacheWrites();
    const { rows, timing } = await timedSearch(q);
    expect(rows.length).toBeGreaterThan(0);
    expect(timing!.cache).toBe('hit');
    expect(timing!.stages.cache_lookup).toBeDefined();
    expect(timing!.stages.keyword).toBeUndefined();
    expect(timing!.stages.rerank).toBeUndefined();
  }, 30_000);
});

describe('A-2 + D-3: fail-open and kill switch', () => {
  test('a throwing clock never fails the search', async () => {
    for (const k of Object.keys(delays)) delete delays[k];
    const timer = createStageTimer();
    expect(timer).not.toBeNull();
    _setStageTimingClockForTests(() => { throw new Error('clock exploded'); });
    try {
      const result = await runWithStageTimer(timer, () =>
        dispatchToolCall(engine, 'search', { query: 'builder latency broken clock', limit: 5 }, {
          remote: true, transport: 'http', sourceId: 'default',
        }),
      );
      expect(result.isError).toBeFalsy();
      expect((JSON.parse(result.content[0]!.text) as unknown[]).length).toBeGreaterThan(0);
      expect(Object.keys(timer!.stages)).toEqual([]);
      // finalize still yields a row value (no stages, latency all unattributed).
      expect(finalizeStageTiming(timer, 12)).toMatchObject({ stages: {}, unattributed_ms: 12 });
    } finally {
      _setStageTimingClockForTests(null);
    }
  }, 30_000);

  test('GBRAIN_STAGE_TIMING=off creates no timer; search runs untimed', async () => {
    process.env.GBRAIN_STAGE_TIMING = 'off';
    try {
      expect(createStageTimer()).toBeNull();
      const { rows, timing } = await timedSearch('builder latency kill switch');
      expect(rows.length).toBeGreaterThan(0);
      expect(timing).toBeNull();
    } finally {
      delete process.env.GBRAIN_STAGE_TIMING;
    }
  }, 30_000);
});

describe('ledger row', () => {
  const row = {
    tokenName: 'client-x', agentName: 'rail-test', operation: 'search', latencyMs: 1234,
    status: 'success', params: { query_len: 5 },
  };

  test('stage_timing lands in mcp_request_log', async () => {
    await insertToolCallLog(engine, row, { v: 1, stages: { rerank: 900 }, unattributed_ms: 4, cache: 'miss' });
    const got = await engine.executeRaw<{ t: Record<string, unknown> }>(
      `SELECT stage_timing AS t FROM mcp_request_log WHERE agent_name = 'rail-test' ORDER BY id DESC LIMIT 1`,
    );
    expect(got[0]!.t).toMatchObject({ stages: { rerank: 900 }, cache: 'miss' });
  });

  test('without the v147 column the row is still written (timing dropped)', async () => {
    await engine.executeRaw(`ALTER TABLE mcp_request_log DROP COLUMN stage_timing`);
    try {
      await insertToolCallLog(engine, { ...row, agentName: 'rail-nocol' }, { v: 1, stages: {}, unattributed_ms: 0 });
      const got = await engine.executeRaw<{ n: number }>(
        `SELECT count(*)::int AS n FROM mcp_request_log WHERE agent_name = 'rail-nocol'`,
      );
      expect(got[0]!.n).toBe(1);
    } finally {
      await engine.executeRaw(`ALTER TABLE mcp_request_log ADD COLUMN IF NOT EXISTS stage_timing JSONB`);
    }
  });
});

describe('D-4: scripts/search-stage-report.sql', () => {
  test('percentiles per stage and the top stage of each slow request', async () => {
    const mk = (latency: number, stages: Record<string, number>, unattributed: number) =>
      insertToolCallLog(engine, {
        tokenName: 'c', agentName: 'rail-report', operation: 'search', latencyMs: latency, status: 'success', params: null,
      }, { v: 1, stages, unattributed_ms: unattributed, cache: 'miss' });
    await mk(12200, { rerank: 3100, embed: 8900 }, 200);
    await mk(4000, { rerank: 2500, embed: 1400 }, 100);
    await mk(9000, { rerank: 1000, embed: 500 }, 7500);

    const sql = readFileSync(join(import.meta.dir, '..', 'scripts', 'search-stage-report.sql'), 'utf8')
      .replaceAll('__WINDOW__', '1 hour')
      .replaceAll('__THRESHOLD_MS__', '8000')
      .replaceAll('__OPERATIONS__', "'search'");
    const [q1, q2] = sql.split('-- @@');
    const pct = await engine.executeRaw<{ stage: string; n: number; p50: number; max: number }>(
      `${q1!.trim().replace(/;$/, '')}`,
    );
    const embed = pct.find((r) => r.stage === 'embed');
    expect(Number(embed!.max)).toBe(8900);
    expect(pct.some((r) => r.stage === '(unattributed)')).toBe(true);

    const slow = await engine.executeRaw<{ latency_ms: number; top_stage: string; top_ms: number }>(
      `${q2!.trim().replace(/;$/, '')}`,
    );
    const byLatency = new Map(slow.map((r) => [Number(r.latency_ms), r.top_stage]));
    expect(byLatency.get(12200)).toBe('embed');
    expect(byLatency.get(9000)).toBe('(unattributed)');
    expect(byLatency.has(4000)).toBe(false);
  });
});
