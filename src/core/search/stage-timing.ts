/**
 * Per-stage wall-clock timing for MCP `search` / `query` requests (fork-local,
 * ANG-Ventures t_c05f77e3).
 *
 * Why: mcp_request_log records one latency_ms per call and nothing else, so a
 * 12 s search could not be attributed to a stage after the fact. serve-http
 * opens a StageTimer per /mcp request, runs the tool dispatch inside it, and
 * writes the finalized breakdown into `mcp_request_log.stage_timing` (JSONB,
 * migration v147).
 *
 * Contract:
 *   - Stages are EXCLUSIVE wall-clock spans around sequential awaits in the
 *     pipeline. A stage opened inside another stage is not recorded (the
 *     outer one already covers it), so sum(stages) never double-counts.
 *     Repeated stages with the same name accumulate (e.g. `config`).
 *   - `unattributed_ms` = latency_ms - sum(stages): the time no stage covers.
 *     It is stored, never hidden, so a gap in the instrumentation is visible.
 *   - Fail-open: every bookkeeping step is wrapped; a timing failure only
 *     loses timing. The wrapped work runs exactly once either way, and its
 *     result or error passes through unchanged.
 *   - Kill switch: GBRAIN_STAGE_TIMING=off|0|false disables timer creation;
 *     every wrap then degrades to a plain call.
 *   - Outside a timer context (CLI, tests, eval replay) every wrap is a plain
 *     call. No query text is ever stored: only durations, counts and the
 *     query length.
 */

import { AsyncLocalStorage } from 'node:async_hooks';
import type { BrainEngine } from '../engine.ts';
import { executeRawJsonb } from '../sql-query.ts';

export const STAGE_TIMING_VERSION = 1;

export type StageInfoValue = number | string | boolean;

export class StageTimer {
  readonly startedAt: number;
  readonly stages: Record<string, number> = {};
  readonly info: Record<string, StageInfoValue> = {};
  constructor() {
    this.startedAt = clock();
  }
}

interface StageStore {
  timer: StageTimer;
  inStage: boolean;
}

const store = new AsyncLocalStorage<StageStore>();

let clockImpl: () => number = () => performance.now();
function clock(): number {
  return clockImpl();
}

/** Test hook: replace the clock (a throwing clock proves fail-open). `null` restores. */
export function _setStageTimingClockForTests(fn: (() => number) | null): void {
  clockImpl = fn ?? (() => performance.now());
}

export function stageTimingEnabled(): boolean {
  const v = (process.env.GBRAIN_STAGE_TIMING ?? '').trim().toLowerCase();
  return !(v === 'off' || v === '0' || v === 'false');
}

/** A new timer, or null when disabled or when timer creation itself fails. */
export function createStageTimer(): StageTimer | null {
  try {
    return stageTimingEnabled() ? new StageTimer() : null;
  } catch {
    return null;
  }
}

/** Run `fn` with `timer` as the active timer (plain call when timer is null). */
export function runWithStageTimer<T>(timer: StageTimer | null, fn: () => T): T {
  if (!timer) return fn();
  return store.run({ timer, inStage: false }, fn);
}

function currentStore(): StageStore | undefined {
  try {
    return store.getStore();
  } catch {
    return undefined;
  }
}

/** Add (now - t0) to `name`. Never throws. */
export function recordSpan(timer: StageTimer | null | undefined, name: string, t0: number): void {
  if (!timer) return;
  try {
    const ms = clock() - t0;
    if (Number.isFinite(ms)) timer.stages[name] = (timer.stages[name] ?? 0) + ms;
  } catch {
    /* fail-open */
  }
}

/** Clock reading for recordSpan; NaN on failure (recordSpan then drops the span). */
export function stageClock(): number {
  try {
    return clock();
  } catch {
    return Number.NaN;
  }
}

/** Time an async stage. Plain call when no timer is active or a stage is already open. */
export function timeStage<T>(name: string, fn: () => Promise<T>): Promise<T> {
  const s = currentStore();
  if (!s || s.inStage) return fn();
  let t0: number;
  try {
    t0 = clock();
  } catch {
    return fn();
  }
  const timer = s.timer;
  return store.run({ timer, inStage: true }, fn).then(
    (v) => {
      recordSpan(timer, name, t0);
      return v;
    },
    (e) => {
      recordSpan(timer, name, t0);
      throw e;
    },
  );
}

/** Time a synchronous stage (same rules as timeStage). */
export function timeStageSync<T>(name: string, fn: () => T): T {
  const s = currentStore();
  if (!s || s.inStage) return fn();
  let t0: number;
  try {
    t0 = clock();
  } catch {
    return fn();
  }
  try {
    return fn();
  } finally {
    recordSpan(s.timer, name, t0);
  }
}

/** Attach a count/label (candidate counts, cache status, query length). Never throws. */
export function setStageInfo(key: string, value: StageInfoValue): void {
  try {
    const s = store.getStore();
    if (s) s.timer.info[key] = value;
  } catch {
    /* fail-open */
  }
}

const round1 = (n: number): number => Math.round(n * 10) / 10;

/**
 * The JSON stored in mcp_request_log.stage_timing, or null on any failure.
 * `latencyMs` is the row's latency_ms, so unattributed_ms reconciles with it.
 */
export function finalizeStageTiming(
  timer: StageTimer | null | undefined,
  latencyMs: number,
): Record<string, unknown> | null {
  if (!timer) return null;
  try {
    const stages: Record<string, number> = {};
    let sum = 0;
    for (const [k, v] of Object.entries(timer.stages)) {
      stages[k] = round1(v);
      sum += v;
    }
    return {
      v: STAGE_TIMING_VERSION,
      stages,
      unattributed_ms: round1(latencyMs - sum),
      ...timer.info,
    };
  } catch {
    return null;
  }
}

/** Ops whose mcp_request_log row carries stage_timing. */
export const STAGE_TIMED_OPS: ReadonlySet<string> = new Set(['search', 'query']);

export interface ToolCallLogRow {
  tokenName: string | null;
  agentName: string | null;
  operation: string;
  latencyMs: number;
  status: string;
  errorMessage?: string | null;
  params: unknown;
}

/**
 * Insert one tools/call row into mcp_request_log, with stage_timing when
 * present. If that insert fails (brain not yet migrated to v147, or a bad
 * timing value), the row is written again WITHOUT stage_timing, so timing can
 * never cost the ledger row. Callers keep their own outer best-effort catch.
 */
export async function insertToolCallLog(
  engine: BrainEngine,
  row: ToolCallLogRow,
  timing: Record<string, unknown> | null,
): Promise<void> {
  const scalars = [row.tokenName, row.agentName, row.operation, row.latencyMs, row.status, row.errorMessage ?? null];
  if (timing) {
    try {
      await executeRawJsonb(
        engine,
        `INSERT INTO mcp_request_log (token_name, agent_name, operation, latency_ms, status, error_message, params, stage_timing)
         VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)`,
        scalars,
        [row.params ?? null, timing],
      );
      return;
    } catch {
      /* fall through: write the row without timing */
    }
  }
  await executeRawJsonb(
    engine,
    `INSERT INTO mcp_request_log (token_name, agent_name, operation, latency_ms, status, error_message, params)
     VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)`,
    scalars,
    [row.params ?? null],
  );
}
