/**
 * PostgresEngine.getAllConfig caches its whole-table snapshot in-process
 * (t_209422a7). gbrain-pg ran `SELECT key, value FROM config` 50,943 times a
 * day at 2.5 ms (the table holds a 1.2 MB row), almost all from the long-lived
 * `gbrain serve`. Pins: one read per TTL, a write on this engine drops the
 * cache, a write during an in-flight read is not lost, TTL 0 disables it, and
 * callers get copies. Pure: a fake `sql`, no DB.
 */

import { describe, it, expect } from 'bun:test';
import { withEnv } from './helpers/with-env.ts';
import { PostgresEngine } from '../src/core/postgres-engine.ts';
import { configSnapshotTtlMs } from '../src/core/postgres-engine/config-snapshot-cache.ts';

function makeEngine() {
  const table: Record<string, string> = { a: '1', b: '2' };
  let reads = 0;
  let gate: Promise<void> | null = null;
  const sql = async (strings: TemplateStringsArray, ...vals: unknown[]) => {
    const q = strings.join('?');
    if (q.includes('SELECT key, value FROM config')) {
      reads++;
      const snap = Object.entries(table).map(([key, value]) => ({ key, value }));
      if (gate) await gate;
      return snap;
    }
    if (q.includes('INSERT INTO config')) { table[vals[0] as string] = vals[1] as string; return []; }
    if (q.includes('DELETE FROM config')) {
      const had = (vals[0] as string) in table;
      delete table[vals[0] as string];
      return { count: had ? 1 : 0 };
    }
    throw new Error('unexpected query: ' + q);
  };
  const e = new PostgresEngine();
  (e as unknown as { _sql: unknown })._sql = sql;
  return {
    engine: e,
    reads: () => reads,
    hold: () => { let release!: () => void; gate = new Promise(r => { release = r; }); return () => { gate = null; release(); }; },
  };
}

// Every test pins the TTL env through withEnv (undefined = unset = default 10 s).
const ttl = (v: string | undefined, fn: () => Promise<void>) => withEnv({ GBRAIN_CONFIG_SNAPSHOT_TTL_MS: v }, fn);

describe('PostgresEngine.getAllConfig snapshot cache (t_209422a7)', () => {
  it('serves repeat reads inside the TTL from memory', () => ttl(undefined, async () => {
    const { engine, reads } = makeEngine();
    expect(await engine.getAllConfig()).toEqual({ a: '1', b: '2' });
    expect(await engine.getAllConfig()).toEqual({ a: '1', b: '2' });
    expect(await engine.getAllConfig()).toEqual({ a: '1', b: '2' });
    expect(reads()).toBe(1);
  }));

  it('setConfig and unsetConfig on the engine drop the cache', () => ttl(undefined, async () => {
    const { engine, reads } = makeEngine();
    await engine.getAllConfig();
    await engine.setConfig('c', '3');
    expect(await engine.getAllConfig()).toEqual({ a: '1', b: '2', c: '3' });
    await engine.unsetConfig('a');
    expect(await engine.getAllConfig()).toEqual({ b: '2', c: '3' });
    expect(reads()).toBe(3);
  }));

  it('does not cache a read that a write overtook', () => ttl(undefined, async () => {
    const { engine, hold } = makeEngine();
    const release = hold();
    const inflight = engine.getAllConfig(); // snapshot taken before the write
    await engine.setConfig('a', 'new');
    release();
    expect(await inflight).toEqual({ a: '1', b: '2' });
    expect(await engine.getAllConfig()).toEqual({ a: 'new', b: '2' });
  }));

  it('TTL 0 disables the cache', () => ttl('0', async () => {
    const { engine, reads } = makeEngine();
    await engine.getAllConfig();
    await engine.getAllConfig();
    expect(reads()).toBe(2);
  }));

  it('expires after the TTL', () => ttl('20', async () => {
    const { engine, reads } = makeEngine();
    await engine.getAllConfig();
    await new Promise(r => setTimeout(r, 40));
    await engine.getAllConfig();
    expect(reads()).toBe(2);
  }));

  it('returns copies: mutating a result does not change the cache', () => ttl(undefined, async () => {
    const { engine } = makeEngine();
    const first = await engine.getAllConfig();
    first.a = 'mutated';
    expect((await engine.getAllConfig()).a).toBe('1');
  }));

  it('TTL parsing: default 10 s, bad values fall back', () => {
    expect(configSnapshotTtlMs({})).toBe(10_000);
    expect(configSnapshotTtlMs({ GBRAIN_CONFIG_SNAPSHOT_TTL_MS: '0' })).toBe(0);
    expect(configSnapshotTtlMs({ GBRAIN_CONFIG_SNAPSHOT_TTL_MS: '2500' })).toBe(2500);
    expect(configSnapshotTtlMs({ GBRAIN_CONFIG_SNAPSHOT_TTL_MS: '-5' })).toBe(10_000);
    expect(configSnapshotTtlMs({ GBRAIN_CONFIG_SNAPSHOT_TTL_MS: 'soon' })).toBe(10_000);
  });
});
