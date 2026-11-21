/**
 * In-process cache for PostgresEngine.getAllConfig (t_209422a7).
 *
 * gbrain-pg ran `SELECT key, value FROM config` 50,943 times a day at 2.5 ms
 * each: the table holds a 1.2 MB row and a long-lived `gbrain serve` resolves
 * config on every request. The snapshot is kept for GBRAIN_CONFIG_SNAPSHOT_TTL_MS
 * (default 10000; 0 disables). setConfig/unsetConfig on the same engine call
 * drop(); a write from another process is seen within the TTL. A generation
 * counter keeps a read that a write overtook out of the cache. Callers get a
 * copy, so mutating a result never touches the cache.
 */

/** TTL for the snapshot. Bad values fall back to the default. */
export function configSnapshotTtlMs(env: Record<string, string | undefined> = process.env): number {
  const raw = (env.GBRAIN_CONFIG_SNAPSHOT_TTL_MS ?? '').trim();
  if (raw === '') return 10_000;
  const n = Number(raw);
  return Number.isInteger(n) && n >= 0 ? n : 10_000;
}

export class ConfigSnapshotCache {
  private entry: { at: number; rows: Record<string, string> } | null = null;
  private gen = 0;

  async get(load: () => Promise<Record<string, string>>): Promise<Record<string, string>> {
    const ttl = configSnapshotTtlMs();
    const hit = this.entry;
    if (ttl > 0 && hit && Date.now() - hit.at < ttl) return { ...hit.rows };
    const gen = this.gen;
    const rows = await load();
    if (ttl > 0 && gen === this.gen) this.entry = { at: Date.now(), rows };
    return { ...rows };
  }

  drop(): void {
    this.entry = null;
    this.gen++;
  }
}

const caches = new WeakMap<object, ConfigSnapshotCache>();

/** The cache owned by one engine instance (no field on the engine class). */
export function configSnapshot(owner: object): ConfigSnapshotCache {
  let c = caches.get(owner);
  if (!c) caches.set(owner, (c = new ConfigSnapshotCache()));
  return c;
}
