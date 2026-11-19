/**
 * Per-caller search budget (fork-local, ANG-Ventures t_b0ef077a).
 *
 * Why: a caller with a hard client deadline (the mem0 per-turn prefetch,
 * OAuth client `rail-prod`, cuts at 15 s and then drops the WHOLE answer)
 * was getting nothing when the reranker ran long. With a budget, the search
 * op hands hybridSearch an absolute deadline; the reranker call is capped to
 * the time left (or skipped when none is left) and the un-reranked top-k is
 * returned, stamped `rerank_budget` in meta.degraded.
 *
 * Config (DB plane, read per call, fail-open to "no budget"):
 *   gbrain config set search.caller_budget_ms '{"rail-prod":12000}'
 * Keys are OAuth client names (mcp_request_log.agent_name). Values are whole
 * milliseconds from op start; anything else is ignored.
 */
import type { BrainEngine } from '../engine.ts';

export const CALLER_BUDGET_CONFIG_KEY = 'search.caller_budget_ms';

/** Smallest rerank window worth opening; below it the reranker is skipped. */
export const MIN_RERANK_WINDOW_MS = 300;
/** Time kept back for the post-rerank stages (alias hop, exact tier, format). */
export const POST_RERANK_RESERVE_MS = 250;

export function parseCallerBudgets(raw: string | null | undefined): Record<string, number> {
  if (!raw) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {};
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
    if (typeof v === 'number' && Number.isFinite(v) && v > 0) out[k] = Math.floor(v);
  }
  return out;
}

/** Budget in ms for this caller, or undefined (no budget). Never throws. */
export async function resolveCallerBudgetMs(
  engine: BrainEngine,
  callerName: string | null | undefined,
): Promise<number | undefined> {
  if (!callerName) return undefined;
  try {
    const raw = await engine.getConfig(CALLER_BUDGET_CONFIG_KEY);
    return parseCallerBudgets(raw)[callerName];
  } catch {
    return undefined;
  }
}

/**
 * What the reranker may do given an absolute deadline (Date.now() ms).
 * `skip`: no usable window left. Otherwise the capped timeout (never above
 * `timeoutMs`) and whether the cap came from the budget.
 */
export function rerankWindow(
  deadlineAt: number | undefined,
  timeoutMs: number | undefined,
  now: number = Date.now(),
): { skip: true } | { skip: false; timeoutMs: number | undefined; budgetCapped: boolean } {
  if (deadlineAt === undefined || !Number.isFinite(deadlineAt)) {
    return { skip: false, timeoutMs, budgetCapped: false };
  }
  const left = deadlineAt - now - POST_RERANK_RESERVE_MS;
  if (left < MIN_RERANK_WINDOW_MS) return { skip: true };
  if (timeoutMs !== undefined && timeoutMs <= left) {
    return { skip: false, timeoutMs, budgetCapped: false };
  }
  return { skip: false, timeoutMs: Math.floor(left), budgetCapped: true };
}
