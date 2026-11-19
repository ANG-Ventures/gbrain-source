/**
 * t_b0ef077a — per-caller search budget + lexical query cap.
 *
 * Pure: parseCallerBudgets, rerankWindow, capLexicalQuery.
 * End-to-end (PGLite, stubbed embed + rerank transports, same rig as
 * hybrid-reranker-skipped.serial.test.ts): with a deadline the reranker is
 * cut to the time left and the un-reranked order comes back stamped
 * `rerank_budget`, with no reranker-failure audit row; with no deadline the
 * reranker runs as before. Serial: mutates the module-global gateway.
 */
import { afterAll, afterEach, beforeAll, describe, expect, test } from 'bun:test';
import { existsSync, mkdtempSync, readdirSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import type { HybridSearchMeta, SearchResult } from '../src/core/types.ts';

const { hybridSearch, capLexicalQuery, LEXICAL_QUERY_MAX_CHARS, awaitPendingSearchCacheWrites } = await import('../src/core/search/hybrid.ts');
const {
  parseCallerBudgets,
  rerankWindow,
  resolveCallerBudgetMs,
  CALLER_BUDGET_CONFIG_KEY,
  MIN_RERANK_WINDOW_MS,
  POST_RERANK_RESERVE_MS,
} = await import('../src/core/search/caller-budget.ts');
const {
  configureGateway,
  resetGateway,
  __setEmbedTransportForTests,
  __setRerankTransportForTests,
} = await import('../src/core/ai/gateway.ts');
const { PGLiteEngine } = await import('../src/core/pglite-engine.ts');

describe('parseCallerBudgets', () => {
  test('keeps positive finite numbers, floors them, drops the rest', () => {
    expect(parseCallerBudgets('{"rail-prod":12000.7,"x":0,"y":-5,"z":"9","w":null}')).toEqual({ 'rail-prod': 12000 });
  });
  test('garbage and non-objects are no budget', () => {
    expect(parseCallerBudgets(null)).toEqual({});
    expect(parseCallerBudgets('')).toEqual({});
    expect(parseCallerBudgets('not json')).toEqual({});
    expect(parseCallerBudgets('[1,2]')).toEqual({});
    expect(parseCallerBudgets('12000')).toEqual({});
  });
});

describe('rerankWindow', () => {
  const now = 1_000_000;
  test('no deadline: timeout unchanged, not capped', () => {
    expect(rerankWindow(undefined, 30_000, now)).toEqual({ skip: false, timeoutMs: 30_000, budgetCapped: false });
  });
  test('plenty of budget: provider timeout stands', () => {
    expect(rerankWindow(now + 60_000, 30_000, now)).toEqual({ skip: false, timeoutMs: 30_000, budgetCapped: false });
  });
  test('budget shorter than the provider timeout: capped to what is left minus the reserve', () => {
    expect(rerankWindow(now + 5_000, 30_000, now)).toEqual({
      skip: false, timeoutMs: 5_000 - POST_RERANK_RESERVE_MS, budgetCapped: true,
    });
  });
  test('less than the minimum window left (or already past): skip', () => {
    expect(rerankWindow(now + POST_RERANK_RESERVE_MS + MIN_RERANK_WINDOW_MS - 1, 30_000, now)).toEqual({ skip: true });
    expect(rerankWindow(now - 1, 30_000, now)).toEqual({ skip: true });
  });
});

describe('capLexicalQuery', () => {
  test('short queries pass through unchanged', () => {
    expect(capLexicalQuery('kitchen lights')).toBe('kitchen lights');
    const exact = 'a'.repeat(LEXICAL_QUERY_MAX_CHARS);
    expect(capLexicalQuery(exact)).toBe(exact);
  });
  test('long queries are cut at a word boundary within the cap', () => {
    const long = Array.from({ length: 2000 }, (_, i) => `word${i}`).join(' ');
    const cut = capLexicalQuery(long);
    expect(cut.length).toBeLessThanOrEqual(LEXICAL_QUERY_MAX_CHARS);
    expect(cut.length).toBeGreaterThan(LEXICAL_QUERY_MAX_CHARS * 0.8);
    expect(long.startsWith(cut)).toBe(true);
    expect(long[cut.length]).toBe(' ');
  });
  test('one unbroken token longer than the cap is hard-cut', () => {
    expect(capLexicalQuery('x'.repeat(5000))).toHaveLength(LEXICAL_QUERY_MAX_CHARS);
  });
});

let engine: InstanceType<typeof PGLiteEngine>;
let tmpHome: string;
const savedGbrainHome = process.env.GBRAIN_HOME;
const savedAuditDir = process.env.GBRAIN_AUDIT_DIR;

function configure(): void {
  resetGateway();
  configureGateway({
    embedding_model: 'openai:text-embedding-3-large',
    embedding_dimensions: 1536,
    env: { OPENAI_API_KEY: 'sk-fake', VOYAGE_API_KEY: 'pa-fake' },
  });
  __setEmbedTransportForTests((async (args: any) => ({
    embeddings: Array.from({ length: args.values.length }, () => Array.from({ length: 1536 }, () => 0.1)),
  })) as any);
}

/** A reranker that answers after `delayMs`, or rejects as soon as the caller aborts. */
function slowReranker(delayMs: number, calls: { n: number }) {
  return async (_url: unknown, init?: RequestInit) => {
    calls.n++;
    const body = JSON.parse(String(init?.body ?? '{}'));
    const n = Array.isArray(body.documents) ? body.documents.length : 0;
    await new Promise<void>((resolve, reject) => {
      const t = setTimeout(resolve, delayMs);
      init?.signal?.addEventListener('abort', () => { clearTimeout(t); reject(new DOMException('aborted', 'AbortError')); });
    });
    return new Response(
      JSON.stringify({ results: Array.from({ length: n }, (_, i) => ({ index: i, relevance_score: 0.9 - i * 0.1 })) }),
      { status: 200, headers: { 'content-type': 'application/json' } },
    );
  };
}

function failureAuditFiles(): string[] {
  const dir = process.env.GBRAIN_AUDIT_DIR!;
  return existsSync(dir) ? readdirSync(dir).filter((f) => f.startsWith('rerank-failures')) : [];
}

beforeAll(async () => {
  tmpHome = mkdtempSync(join(tmpdir(), 'gbrain-caller-budget-'));
  process.env.GBRAIN_HOME = tmpHome;
  process.env.GBRAIN_AUDIT_DIR = join(tmpHome, 'audit');
  engine = new PGLiteEngine();
  await engine.connect({});
  await engine.initSchema();
  for (const [slug, title, type] of [
    ['alice-foo', 'Alice Foo', 'person'],
    ['bob-bar', 'Bob Bar', 'company'],
    ['carol-baz', 'Carol Baz', 'note'],
  ] as const) {
    const truth = `${title} is a builder shipping reranker plumbing.`;
    await engine.putPage(slug, { type, title, compiled_truth: truth });
    await engine.upsertChunks(slug, [{ chunk_index: 0, chunk_text: truth, chunk_source: 'compiled_truth' }]);
  }
});

afterEach(() => {
  __setRerankTransportForTests(null);
});

afterAll(async () => {
  __setEmbedTransportForTests(null);
  __setRerankTransportForTests(null);
  resetGateway();
  if (savedGbrainHome === undefined) delete process.env.GBRAIN_HOME;
  else process.env.GBRAIN_HOME = savedGbrainHome;
  if (savedAuditDir === undefined) delete process.env.GBRAIN_AUDIT_DIR;
  else process.env.GBRAIN_AUDIT_DIR = savedAuditDir;
  try { await engine.disconnect(); } catch { /* ignore */ }
  try { rmSync(tmpHome, { recursive: true, force: true }); } catch { /* ignore */ }
});

async function run(deadlineAt?: number): Promise<{ results: SearchResult[]; meta: HybridSearchMeta; ms: number }> {
  let meta: HybridSearchMeta | undefined;
  const t0 = Date.now();
  const results = await hybridSearch(engine, 'builder', {
    limit: 5, ...(deadlineAt !== undefined ? { deadlineAt } : {}), onMeta: (m) => { meta = m; },
  });
  if (!meta) throw new Error('onMeta never fired');
  return { results, meta, ms: Date.now() - t0 };
}

describe('hybridSearch with a caller deadline (t_b0ef077a)', () => {
  test('no deadline: a slow reranker still runs to completion (behavior unchanged)', async () => {
    configure();
    const calls = { n: 0 };
    __setRerankTransportForTests(slowReranker(400, calls) as any);
    const { results, meta } = await run();
    expect(calls.n).toBe(1);
    expect(results[0]!.rerank_score).toBe(0.9);
    expect((meta.degraded ?? []).some((d) => d.stage === 'rerank_budget')).toBe(false);
  });

  test('deadline shorter than the reranker: cut at the budget, RRF order back, stamped timeout, no failure audit', async () => {
    configure();
    const calls = { n: 0 };
    __setRerankTransportForTests(slowReranker(5_000, calls) as any);
    const before = failureAuditFiles().length;
    const { results, meta, ms } = await run(Date.now() + 1_200);
    expect(calls.n).toBe(1);
    expect(results.length).toBeGreaterThan(0);
    expect(results.every((r) => r.rerank_score === undefined)).toBe(true);
    expect(meta.degraded ?? []).toContainEqual({ stage: 'rerank_budget', reason: 'timeout' });
    expect(ms).toBeLessThan(3_000);
    expect(failureAuditFiles().length).toBe(before);
  });

  test('deadline already spent: reranker not called, stamped budget_exhausted', async () => {
    configure();
    const calls = { n: 0 };
    __setRerankTransportForTests(slowReranker(10, calls) as any);
    const { results, meta } = await run(Date.now() - 1);
    expect(calls.n).toBe(0);
    expect(results.length).toBeGreaterThan(0);
    expect(meta.degraded ?? []).toContainEqual({ stage: 'rerank_budget', reason: 'budget_exhausted' });
  });

  test('resolveCallerBudgetMs reads search.caller_budget_ms per caller name', async () => {
    await engine.setConfig(CALLER_BUDGET_CONFIG_KEY, '{"rail-prod":12000}');
    expect(await resolveCallerBudgetMs(engine, 'rail-prod')).toBe(12000);
    expect(await resolveCallerBudgetMs(engine, 'rail-cli')).toBeUndefined();
    expect(await resolveCallerBudgetMs(engine, undefined)).toBeUndefined();
    await engine.setConfig(CALLER_BUDGET_CONFIG_KEY, 'garbage');
    expect(await resolveCallerBudgetMs(engine, 'rail-prod')).toBeUndefined();
  });

  test('search op: the budgeted caller gets the un-reranked answer, another caller is reranked', async () => {
    const { operations } = await import('../src/core/operations.ts');
    const search = operations.find((o) => o.name === 'search')!;
    const ctxFor = (clientName: string) => ({
      engine, config: {}, logger: console, dryRun: false, remote: false, sourceId: 'default',
      auth: { token: 't', clientId: `id-${clientName}`, clientName, scopes: ['read'] },
    }) as any;
    configure();
    // 1 ms budget: spent before the rerank stage, so the reranker is never called.
    await engine.setConfig(CALLER_BUDGET_CONFIG_KEY, '{"rail-prod":1}');
    await engine.executeRaw('DELETE FROM query_cache');
    const calls = { n: 0 };
    __setRerankTransportForTests(slowReranker(10, calls) as any);
    const budgeted = (await search.handler(ctxFor('rail-prod'), { query: 'builder shipping', limit: 3 })) as SearchResult[];
    expect(budgeted.length).toBeGreaterThan(0);
    expect(calls.n).toBe(0);
    expect(budgeted.every((r) => r.rerank_score === undefined)).toBe(true);
    // The stub embedder gives every query the same vector, so drain the first
    // call's (degraded, short-TTL) cache write before clearing the cache.
    await awaitPendingSearchCacheWrites();
    await engine.executeRaw('DELETE FROM query_cache');
    const other = (await search.handler(ctxFor('rail-cli'), { query: 'builder plumbing', limit: 3 })) as SearchResult[];
    expect(calls.n).toBe(1);
    expect(other[0]!.rerank_score).toBe(0.9);
    await engine.unsetConfig(CALLER_BUDGET_CONFIG_KEY);
  });
});
