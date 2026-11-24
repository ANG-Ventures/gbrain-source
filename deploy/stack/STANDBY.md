# STANDBY.md: the gbrain warm standby on nas-fleet and the one swap command (card t_dd1e1edb)

The Mac Studio is the gbrain primary; nas-fleet holds a warm standby; swapping the two is one command (owner ruling,
Discord 1573571257654579233). The shape and the fixed choices are the mem0 standby's (mem0-selfhost-deploy
docs/STANDBY.md, docs/SWAP.md): an async physical replica with a page past 60 s of lag, its own NAS dataset, manual
promotion only, one cron ever, fence before promote, a real query as the proof, drills on copies before the operator's
production drill. The code is a port of mem0ctl's (`gbrain_standby.py` = lib/mem0ctl/standby.py, `gbrain_swap.py` =
lib/mem0ctl/swap.py, `standby/primary-enable.sh` = ops/standby/primary-enable.sh), reached as `gbrainctl standby ...`
and `gbrainctl swap ...`. The doctor's last line has mem0's exact shape, so one readout serves both stores
(hermes-home `scripts/standby-status.py`).

| piece | where | how it stays current |
|---|---|---|
| Postgres 16 (pgvector) | nas-fleet `gbrain-pg` in compose project `gbrain`, data on dataset `tank/gbrain-standby` (`/srv/gbrain-standby/pg`, docker volume `gbrainstandby_pg`) | physical streaming replica, slot `gbrain_standby_nas_fleet`, hot standby, async |
| server + cron | nas-fleet `gbrain-server-1`, `gbrain-cron-1` | CREATED, never started (compose profile `promoted`) |
| rerank-proxy + rerank-tunnel | nas-fleet | RUNNING: stateless; the doctor reads `primary_ok` so the GPU path from the NAS is proven before a promotion needs it |
| the link | nas-fleet `gbrain-primary-link-1` | ssh as `_studiolink@192.168.1.18` (the mem0 standby's login, its own tagged key line), forwards the Studio's loopback 5433 (postgres) and 8199 (serve) onto the standby's compose network; publishes the serve port on this host's loopback |
| the doctor | `python3 ./gbrainctl standby status --host nas-fleet` | run on nas-fleet in `~/gbrain-standby/deploy/stack` |
| the swap | `gbrainctl swap --to <host>` | run from the Studio's deploy checkout |
| the page | hermes-home `scripts/gbrain-standby-watch.py`, cron row `dd1e1edb0001` (every 5 min, ships disabled) | episode pager, `lag` / `doctor` |

## Where gbrain differs from mem0, and why

- **One Postgres, no graph.** Fence and catchup prove the Postgres shutdown checkpoint only; the doctor has no graph
  line.
- **29-30 GB instead of 6 GB.** The seed is a 30 GB `pg_basebackup` through the link: 309 s at `-r 100M` in the drill
  (`basebackup_rate`, the read burst the Studio sees). A staged reseed (`--reseed`, the swap's step 8) builds the new
  copy BESIDE the old one, so budget twice the cluster on the dataset (tank/gbrain-standby: 16K records on the SSD
  special mirror, 490 GB free there on 11-24; the drill's copies allocated 28-43 GB each). The Studio's VM disk has
  3.6 TB free for its own staged reseed. `max_slot_wal_keep_size = 8GB` on the primary: gbrain writes ~1.4 GB of WAL a
  day (pg_stat_wal 11-24), so a standby can be down about 5 days before its slot is invalidated and the doctor says
  "reseed" (mem0's cap, same value).
- **The rerank path reaches a GPU host.** zerank-native lives on ACE-AI; the server only calls the in-stack proxy. The
  standby runs its own proxy + tunnel with the same rendered tunnel key, and the drill read `primary_ok=True` from
  nas-fleet: ACE-AI accepts that key from the NAS address. The doctor reports it and never refuses on it: the GPU host
  serves both boxes alike, and the proxy's DeepInfra fallback answers when it is down.
- **20 idle server connections.** gbrain serve holds a postgres.js pool (10-20 idle, `pg_stat_activity` 11-24) that
  writes as it serves (OAuth tokens, `mcp_request_log`, `query_cache`), so the server can never run against a
  read-only replica: it is in the `promoted` profile with the cron, and the fence stops it (and the cron) BEFORE it
  stops Postgres, so the shutdown checkpoint is clean (`pg_controldata` = `shut down`). Those connections are not
  walsenders: the replica's walreceiver uses one of the primary's 10 `max_wal_senders`, not a `max_connections` slot.
- **No DNS name: the flip is the old primary's link.** Every gbrain caller lives on the Studio and calls
  `127.0.0.1:8199` (the mem0 prefetch's rail-prod client, `rail.sh`, the MCP shim). After `swap --to nas-fleet`, the
  Studio (ROLE=standby) runs only its `primary-link`, which publishes 127.0.0.1:8199 and forwards it through the
  reverse link (`_mem0link@192.168.1.154`, the mem0 reverse login, its own tagged key line) to nas-fleet's server. No
  caller config changes. Swap back: the Studio's own server publishes the port again. The cold CLI fallback of
  `rail.sh` (127.0.0.1:5433) reaches the Studio's REPLICA while nas-fleet is the primary: reads work, writes do not.
- **No outage swap.** `--old-primary-is-down` is refused. The callers live on the old primary, so promoting the NAS
  while the Studio is down serves nobody, and a Studio that comes back restarts its own postgres (unless-stopped)
  beside it with every caller still pointed at it. With the Studio lost, the replica holds the data (lag-bounded);
  run the swap when the Studio answers, or rebuild the Studio from the NAS (`standby seed` on a fresh Studio stack).
- **The brain repo stays on the Studio.** The exporters write `~/gbrain/brain` there. nas-fleet mounts an EMPTY git
  repo (`standby init` makes it), so a promoted NAS's 03:30 nightly-sync finds nothing new and imports nothing; the
  Studio's first night after the swap back imports everything (incremental by content hash). pg-backup on a promoted
  NAS writes the same restic repo under the same restic host, so the dump chain continues.
- **What a promoted NAS lacks:** `think` (the claude-apr relay at :18810 is Studio loopback; search, query and graph
  do not use it), ingest of new brain files (above), the host-side `gbrain-stack-watch` (it watches the Studio's
  stack), and the fleet backup-freshness stamp (written on the NAS, read on the Studio).

## 1. Standing it up (operator, after this PR merges)

All of these run with the Studio serving; nothing on the primary is recreated or restarted.

1. **nas-fleet checkout + images** (nas-fleet has no GitHub credentials; carry the merge commit in):
   ```sh
   # on the Studio, from the deploy checkout (M = this PR's merge commit, already fetched there)
   git -C ~/gbrain-stack-live bundle create /tmp/gbs.bundle origin/master
   scp /tmp/gbs.bundle ace@192.168.1.154:/tmp/ && shasum -a 256 /tmp/gbs.bundle
   ssh ace@192.168.1.154 'sha256sum /tmp/gbs.bundle; cd ~/gbrain-standby && git fetch -q /tmp/gbs.bundle master \
     && git checkout -q --detach <M> && git log --oneline -1'
   ssh ace@192.168.1.154 'cd ~/gbrain-standby/deploy/stack && python3 ./gbrainctl build --host nas-fleet'
   #   BUILD=ok revision=7969d968... arch=amd64 (the three images are already there from the drill: id check below)
   ```
   (`~/gbrain-standby` exists on nas-fleet since 11-24, a clone of the bundle; move it with `git checkout --detach`
   only while no standby runs, i.e. before step 4. It is not a stamped deploy tree.)
2. **Render + init on nas-fleet** (render reads 1P with nas-fleet's read-only Engineering token; init prints the two
   NON-secret inputs of the primary step):
   ```sh
   ssh ace@192.168.1.154 'cd ~/gbrain-standby/deploy/stack && python3 ./gbrainctl render --host nas-fleet \
     && python3 ./gbrainctl standby init --host nas-fleet'
   #   RENDER=ok host=nas-fleet keys=26 files=10 ...
   #   STANDBY_KEY_LINE='restrict,port-forwarding,from="192.168.1.154",permitopen="127.0.0.1:5433",permitopen="127.0.0.1:8199" ssh-ed25519 ...'
   #   STANDBY_REPL_VERIFIER='SCRAM-SHA-256$4096:...'
   #   STANDBY_REPL_ROLE=gbrain_repl STANDBY_SLOT=gbrain_standby_nas_fleet STANDBY_MARK='# gbrain-standby t_dd1e1edb'
   #   INIT=ok host=nas-fleet secrets=/home/ace/gbrain-standby/deploy/stack/secrets/standby
   ```
3. **The ONE primary-side step, on the Studio** (online: a role, a cap, one pg_hba line read back through
   `pg_hba_file_rules`, one key line in `/etc/ssh/studiolink_authorized_keys`):
   ```sh
   cd ~/gbrain-stack-live/deploy/stack
   export STANDBY_KEY_LINE='<from step 2>' STANDBY_REPL_VERIFIER='<from step 2>'
   export STANDBY_REPL_ROLE=gbrain_repl STANDBY_SLOT=gbrain_standby_nas_fleet
   standby/primary-enable.sh check     # role ABSENT, hba_loaded 0, slot ABSENT, key_line ABSENT
   standby/primary-enable.sh apply     # ... hba_loaded 1, hba_errors 0, key_line present, APPLY=ok
   ```
   Rollback: stop the standby's postgres + link (`gbrainctl compose --host nas-fleet --allow-prod -- stop postgres
   primary-link` on nas-fleet), then `standby/primary-enable.sh undo` on the Studio (drops the slot and the role, resets
   the cap, removes the hba and key lines; their tag is `# gbrain-standby t_dd1e1edb`).
4. **Seed, on nas-fleet** (~5-6 min at 100M; the Studio sees a 100 MB/s read):
   ```sh
   ssh ace@192.168.1.154 'cd ~/gbrain-standby/deploy/stack && python3 ./gbrainctl standby seed --host nas-fleet \
     && python3 ./gbrainctl standby status --host nas-fleet'
   #   basebackup rc=0 seconds=~310 slot=gbrain_standby_nas_fleet created ... SEED=ok
   #   last line: STANDBY=FRESH lag_s=0.0 ELIGIBLE=yes
   ```
5. **The page**: resume hermes-home cron row `dd1e1edb0001` (gbrain-standby-watch, every 5 min) once step 4 reads
   FRESH. `scripts/standby-status.py` prints one doctor line per store (mem0 and gbrain) any time.

## 2. The swap (operator; attended)

Before the window, from the Studio's deploy checkout:
1. Both checkouts at the same commit (`git -C ~/gbrain-stack-live rev-parse HEAD` = nas-fleet's).
2. `python3.14 ./gbrainctl swap --to nas-fleet --dry-run` -> `SWAP=dry-run id=... {"doctor": "STANDBY=FRESH lag_s=0.0
   ELIGIBLE=yes", "old_pages": <n>, "old_health": {"engine": "postgres", ...}, "callers_host": "studio"}`.
3. The reverse link for step 6/8: nas-fleet already has `_mem0link` (mem0 docs/SWAP.md 3.3); the swap appends the
   Studio's gbrain key line to `/etc/ssh/mem0link_authorized_keys` itself (step 6, `primary-enable.sh apply` on
   nas-fleet with sudo -n). Check: `ssh ace@192.168.1.154 'sudo -n true && id _mem0link'`.
4. Pause the gbrain monitors for the window: hermes-home `gbrain-stack-watch` (70c54de14efe: it would page the
   stopped Studio services) and `gbrain-standby-watch` (dd1e1edb0001).
5. Run it as a detached job with a wake (`~/.hermes/scripts/detached-run.py`), never in a gateway turn:
   `python3.14 ./gbrainctl swap --to nas-fleet`.

Expected lines (the drill's, production names): `fence ... pg_state=shut down`, `catchup ... replay_lsn >=
checkpoint_lsn`, `promote ... pg_in_recovery=False`, `start ... health_ok=True cron_count=1 old_cron_running=0`,
`flip ... old_link={... engine: postgres}`, `prove ... hits>=1`, `reseed_old ... doctor=STANDBY=FRESH lag_s=0.0
ELIGIBLE=yes`, last `SWAP=ok id=<id> to=nas-fleet from=studio steps=8/8 seconds=<s>`.

Caller-visible cost (drill, 11-24, under a search every 0.5 s through the callers' port and a write every 0.5 s): see
"Drill" below. Budget ~20 s of failed searches per direction and ~10 min for step 8 (a 30 GB staged reseed + verify).
Until step 8 finishes there is NO standby; say so in every report.

Swap back: `python3.14 ./gbrainctl swap --to studio --dry-run`, then without `--dry-run` (the Studio follows nas-fleet
through `_mem0link`; step 8 reseeds nas-fleet through `_studiolink`, whose key line the first standby set up).

Stop conditions (the mem0 rules): preflight refused = nothing changed. A step failing before `pg_promote`:
`gbrainctl swap undo <id>` -> `SWAP=undone primary=studio` (the Studio's server and cron back, ARMED as it was). After
promote: `gbrainctl swap resume <id>` only. Callers erroring for more than 120 s from the fence: stop and read the
journal (`~/.local/state/gbrain-swap/<id>/journal.jsonl` on the box that ran it).

After the swap back: resume both monitors, read their first ticks.

## 3. Drill (nas-fleet, on a copy; card t_dd1e1edb, 2026-11-24)

`standby/drill/drill.sh` on nas-fleet as ace: two compose projects of their own (gbrain-swapdrill-a plays the Studio
and the callers' host, gbrain-swapdrill-b plays nas-fleet), a rig sshd shaped like `_studiolink`, data under
`/srv/gbrain-standby/drill-t_dd1e1edb`. Half A started from a `pg_basebackup` COPY of the live gbrain-pg (a throwaway
container sharing gbrain-pg's network namespace, `-X stream -r 100M`, its own temporary slot, into a scratch volume on
the Studio, streamed to the NAS and the volume removed: 322 s backup + 469 s transfer, 49,043 pages). Real images
(amd64 at 7969d968), real 1P render, real rerank tunnel to ACE-AI; the drill cron is never armed and has no NAS key.

Measured 2026-11-24 15:10-15:43Z (`drill.sh setup`, `run`, then an undo arm; journals and logs were read before
`teardown`):

| what | result |
|---|---|
| render + init, both halves | `RENDER=ok keys=26 files=10` each (1P read-only token on nas-fleet), `INIT=ok` |
| A up on the copy | postgres up and answering in 7 s, 49,043 pages; full stack `/health` engine=postgres 5 s later |
| primary-enable (A accepts B) | `role gbrain_repl_drill_b login=true repl=true`, `hba_loaded 1`, `hba_errors 0`, `key_line present`, `APPLY=ok` |
| `standby seed` of B, 30 GB through the ssh link | `basebackup rc=0 seconds=309`, `SEED=ok`, 312 s end to end |
| B's doctor after the seed | `STANDBY=FRESH lag_s=0.0 ELIGIBLE=yes`, server + cron created at revision 7969d968 == pin, `rerank primary_ok=True` (the GPU host reached from nas-fleet) |
| swap 1, A -> B (`SWAP=ok steps=8/8`, 676 s) | preflight 0.7 s, fence 4.6 s (`pg_state=shut down`), catchup 0.2 s (replay 7D/A085700 >= checkpoint 7D/A085688), promote 5.2 s, start 4.4 s (`cron_count=1 old_cron_running=0`), flip 4.5 s, prove 57.0 s (the first search on a cold server: 56.8 s, 1 hit), reseed_old 599 s (`STANDBY=FRESH lag_s=0.0 ELIGIBLE=yes`) |
| callers during swap 1 | searches every 0.5 s through A's loopback serve port: 1,302, 35 failed, longest failure window **17.5 s**; writes every 0.5 s: 1,385, 7 failed, longest window 3.5 s; **acked writes 1,378 / 1,378 present on the new primary** |
| swap 2, B -> A, the swap back (`SWAP=ok steps=8/8`, 542 s) | fence 6.0 s, catchup 0.2 s, promote 4.5 s, start 4.3 s, flip 4.9 s, prove 17.1 s, reseed_old 505 s |
| callers during swap 2 | searches 1,074, 28 failed, longest window **33.6 s** (the fence to the first answer from A's freshly started server: its first searches ran past the 20 s client timeout); writes 1,126, 10 failed, longest 5.0 s; **acked writes 1,116 / 1,116 present** |
| after each swap | the new standby's doctor `STANDBY=FRESH lag_s=0.0 ELIGIBLE=yes`; both serve ports answer engine=postgres and a search (warm: 223-299 ms) |
| undo arm: swap A -> B with an injected failure at promote, then `swap undo` | `SWAP=failed ... step=promote` (preflight, fence, catchup done); `SWAP=undone primary=drill-a` in 15 s (fence to serving again: 20 s); B stayed a FRESH, ELIGIBLE replica; a second `undo` is a no-op `SWAP=undone` |

What the numbers mean for production:
- Callers lose ~20-35 s of searches per direction: fence -> start is ~15 s, and a freshly started server answers its
  first searches slowly (cold caches; 17-57 s for the first in the drill). The mem0 prefetch's gbrain leg times out to
  `[]` for those turns (it does not fail the turn); rail.sh fails for them.
- Writes (the cron's imports, OAuth token rows) are refused for ~5 s and none that was acked is lost.
- reseed_old (a 30 GB staged copy + `pg_verifybackup` + the switch) is 8-10 minutes; there is no standby until it
  ends. The Studio's base backup is read at `basebackup_rate` (100 MB/s).
- The production Studio side adds what the drill cannot: OrbStack, the Studio's 6 GB shared_buffers shutdown
  checkpoint, and a Studio reseed over the real LAN link.

## 4. Changed on TrueNAS / nas-fleet by t_dd1e1edb

- TrueNAS: dataset `tank/gbrain-standby` (recordsize 16K, special_small_blocks 16K, lz4, atime off: the mem0 standby's
  settings), hot-plugged into the `nas-fleet` LXC as device `gbrain-standby` at `/srv/gbrain-standby` (`incus config
  device add`; no restart). Rollback: stop the standby, `incus config device remove nas-fleet gbrain-standby`,
  `zfs destroy -r tank/gbrain-standby`.
- nas-fleet: checkout `~/gbrain-standby` (a clone of a bundle of this PR's branch); the three amd64 images
  `gbrain-{server,rerank,cron}:7969d9685cfb-amd64` (`gbrainctl build --host nas-fleet`, BUILD=ok); `gbrainctl render
  --host nas-fleet` (RENDER=ok keys=26 files=10) and `gbrainctl standby init --host nas-fleet` (INIT=ok: the link key,
  the replication password + its SCRAM verifier in `deploy/stack/secrets/standby/` 0600, docker volume
  `gbrainstandby_pg` bound to `/srv/gbrain-standby/pg`, an empty brain repo at `/srv/gbrain-standby/gbrain/brain`).
  Nothing is started: the doctor reads `STANDBY=STALE lag_s=unknown ELIGIBLE=no reason=replica postgres is not
  running` until step 3 + 4. A re-run of `init` prints the same key line and verifier (both are kept).
  The drill's projects, volumes, rig and data dir were removed (teardown read back 0 containers, 0 volumes).
