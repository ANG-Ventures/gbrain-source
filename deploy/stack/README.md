# gbrain stack: the whole of gbrain from containers (card t_ab2fcba3)

One compose project per host runs everything that used to be launchd jobs, hermes-home cron rows and a hand-run
container on the Mac Studio:

| was (2026-11-20) | is (service) | image |
|---|---|---|
| `ai.gbrain.serve` (launchd, `bun ~/gbrain/app/src/cli.ts serve` from a live checkout) | `server` | `gbrain-server:<sha12>-<arch>` built from a commit |
| `ai.gbrain.rerank-proxy` (launchd, `~/gbrain/rerank-failover-proxy.py`) | `rerank-proxy` | `gbrain-rerank:<sha12>-<arch>` |
| `ai.gbrain.rerank-tunnel` (launchd, `~/.hermes/scripts/gbrain-rerank-tunnel.sh`) | `rerank-tunnel` (profile `tunnel`) | same rerank image |
| `gbrain-pg` (`docker run` by hand, 2026-07-09) | `postgres` (container name still `gbrain-pg`, same volume) | `pgvector/pgvector:pg16@sha256:1d53...` |
| cron rows gbrain-nightly-sync, gbrain-pg-backup, gbrain-deploy-parity | `cron` (supercronic + `gbrainjob`) | `gbrain-cron:<sha12>-<arch>` |

Ports, container name, data volume, restic repo/tags/host and the backup success stamp stay what they were, so no
caller changes: mem0 prefetch and `rail.sh` keep `127.0.0.1:8199`, the launcher CLI and the off-host restore drill
keep `127.0.0.1:5433`, `gbrain-restore-drill.sh` keeps finding `gbrain-pg` snapshots.

Files (mem0-selfhost-deploy's shapes, read its `bin/mem0ctl`, `hosts/*.toml`, `scripts/prod_recreate.sh`):

* `gbrainctl` render / compose / up / down / build / status. stdlib python >= 3.11 (`python3.14 ./gbrainctl` on
  the Studio; the login `python3` there may be Xcode's 3.9). On project `gbrain`, `up`, `down` and every `compose`
  subcommand except the read-only ones (ps, config, logs, images, top, port, ls, version) need `--allow-prod`.
* All three images run as one non-root user, `hosts/<h>.toml` `stack_uid:stack_gid` (Studio 502:20: the owner of
  `~/gbrain/brain` and the user who renders the 0600 secrets), baked in by `gbrainctl build`. Nothing in the stack
  starts as root except the upstream postgres image. The cron's `<gbrain_home>/.gbrain` is a tmpfs owned by that uid.
* `env.schema.toml` every `.env` key and every secret FILE, with its 1Password ref. `hosts/<name>.toml` the only
  place a host IP, port or path lives. `stack/.env` and `secrets/*` are build outputs (0600, gitignored).
* `docker-compose.yaml` + `docker-compose.studio.yaml` (arm64, limits = the live container's) +
  `docker-compose.proof.yaml` (the side-by-side proof only).
* `server/`, `rerank/`, `cron/` the three images. `cron/jobs/*.sh` are container ports of the hermes-home scripts
  (each header says what changed); `cron/lib/ingest-chunked.sh` and `rerank/proxy.py` are copies of the
  ANG-Ventures/gbrain files with only the host couplings made configurable.
* `gbrain-stack-watch` the host-side monitor (docker + GitHub checks the containers cannot do; pages on failed job
  results, the rerank fallback, DeepInfra 401/402 and a stopped rerank-tunnel; once per episode, open findings kept in
  `<receipts_dir>/stack-watch-state.json`). `tests/test_stack.py` the static contract.

## Operating

```
cd <stack_dir>                                    # hosts/<h>.toml stack_dir: a checkout of this repo, deploy/stack
python3.14 ./gbrainctl build  --host studio       # three images from a CLEAN worktree at the pinned revision
python3.14 ./gbrainctl render --host studio       # .env + secrets/* from 1P (6 op-cached-read refs)
python3.14 ./gbrainctl up     --host studio --allow-prod
python3.14 ./gbrainctl status --host studio       # every container's revision label vs the pin
python3.14 ./gbrainctl compose --host studio -- logs --tail 50 server
docker exec -u 502:20 gbrain-cron-1 gbrainjob show            # latest result line per job
docker exec -u 502:20 gbrain-cron-1 gbrainjob run nightly-sync   # run a job now
```

Deploy a new gbrain version = PR that moves `hosts/studio.toml` `revision` + the three image tags to the new master
sha, then `build`, `up`. No more `git -C ~/gbrain/app merge --ff-only` + `launchctl kickstart`.

The cron container is inert until `/state/ARMED` exists (`tick` returns without running), and it holds a Postgres
advisory lock (`pg_try_advisory_lock(7271, crc32('gbrain-cron-instance'))`) for its whole life: a second cron
container against the same database exits 1 instead of running jobs twice. Each job also takes its own
advisory lock for the run, so `gbrainjob run` from a second container skips.

## Proof (2026-11-20, Studio, beside the live launchd service)

`hosts/studio-proof.toml`: project `gbrain-proof`, ports 15433 / 18299 / 18185, volume `gbrain-pgdata-proof` =
`pg_basebackup` of the live `gbrain-pg` (replication connection over its own loopback, `--max-rate=150M`,
21:56 to 22:01 PT, 29 GB), brain repo = an APFS clone (`cp -c -R`, 20 s) mounted at the live path so
`ingest_log.source_ref` matches, scratch restic repo. Images built on the Studio by `gbrainctl build` from
`99aa9fca` (= master `9bd1cfeb` + `deploy/stack` only; `git diff 9bd1cfeb 99aa9fca -- . ':!deploy/stack'` is empty).
Final images: `422c0550` (same app code). Measured 2026-11-20 22:02-23:05 PT:

| check | result |
|---|---|
| same answers, `search` limit 5, 20 real recent Discord turns (what the mem0 prefetch sends) | 20/20 identical slug#chunk_id lists and order, live vs proof; live vs live 20/20 |
| same, with the proof's `query_cache` emptied (62,204 rows -> 0) so every proof search is computed fresh (1.1-5.7 s each), final images | 20/20 identical |
| a second set of 20 newer turns | 20/20 identical |
| health | server `/health` `{"status":"ok","engine":"postgres"}`, rerank-proxy `primary_ok: true`, `gbrainctl status` STATUS=ok |
| nightly-sync (99aa9fca image) | ok, 10 roots, 84 pages updated, brain clone committed; 1,560 s, of which ~20 min was one `git add -A` re-hashing an APFS clone whose mtimes all changed (proof artefact; see the git stat row) |
| nightly-sync again (422c0550, minimal stat checks, a probe page added to the clone) | ok in 411 s; the probe page is in the proof DB; commit authored by the host identity |
| pg-backup into a scratch restic repo | ok, 30.05 GB plain SQL in 196 s (cron at 2 cpus for the run), `restic dump` header `Dumped from 16.14 / by pg_dump 16.16`. Second run FAILED at `forget`: a lock left by a SIGPIPE'd `restic dump` in the previous (recreated) container, whose hostname differed, so `unlock` could not clear it before it aged 30 min. Fix: a stable cron `hostname` (98066431). Third run ok in 102 s, 3.3 MB added |
| deploy-parity, once | ok; with a wrong pin it fails naming both revisions |
| one cron per database | a second cron container exits 1 `another gbrain cron container holds this database's instance lock`; after a DB restart the cron container exits within 30 s and re-takes the lock (RestartCount 1) |
| git over the bind mount (live repo, read-only, `--no-optional-locks`) | container default stat checks 481 s, container `core.checkStat=minimal` 14 s, host native 3 s |
| downtime of a stop -> new containers -> up cycle (proof) | stop to `/health` ok 15.5 s, to first answered search 15.9 s (12.7 s of it is `up`, mostly the postgres healthcheck interval) |

Round 1 re-run (2026-11-21 09:11-10:10 PT, non-root images `fe172fe0` built at 502:20, a FRESH `pg_basebackup` of
live `gbrain-pg`, a new brain clone, 20 real recent Discord turns):

| check | result |
|---|---|
| uid | server, rerank-proxy and cron: image `User 502:20`, PID 1 `Uid 502`; `/state`, `/state/secrets/*`, `.pgpass` 502:20 0600/0700; `<gbrain_home>/.gbrain` tmpfs uid=502 mode 0700 |
| same answers, proof `query_cache` emptied (63,017 rows -> 0) | 20/20 identical, live vs live 20/20; then warm 20/20 |
| health | `gbrainctl up` -> server `/health` engine=postgres in 16.1 s (postgres start included); recreate of the app services only: 9.9 s; rerank `primary_ok: true`; `gbrainctl status` STATUS=ok rc 0 |
| bind mounts and volumes as 502 | nightly-sync ok 611.6 s, 10 roots, commit in the brain clone by the host identity, `.git/index` still 502:20; result files in the host receipts dir 502:20; pg-backup ok 102 s into the scratch restic repo on the cron-state volume; deploy-parity ok |
| .mdx-only night | stamp present, no change: `nothing_new`; one new `.mdx` only: the gate passes, `roots_imported: 10`, the page is in the proof DB (three probes) |
| amd64 | the three images build on ace-ai at uid 1000:1000 (the bun base user's uid: it is removed, getpwuid(1000) = `gbrain`, cron home `/state`) |

One cold compare on the earlier `b63f3b39` images, run AFTER the proof's own nightly import, gave 19/20: the extra
hit was the `shelly` page, which that import had just re-imported from a newer brain file (proof `updated_at`
2026-11-21 17:27Z vs live 2026-11-19). That is the proof DB being ahead of live, not the stack; the fresh-copy run
above is the comparison of record.

Cores and memory, 60 searches per side (20 new turns + 40 repeats), same moment, live = launchd processes, proof =
containers (cgroup): server 3.98 vs 3.16 CPU-s, rerank proxy 0.22 vs 0.08 CPU-s, server memory 364 MiB RSS vs
206 MiB, postgres CPU 6.3 % vs 4.5 % avg (docker stats), postgres memory 3.6 GiB vs 2.2 GiB (the proof's
shared_buffers were colder).

## Before the cutover (operator, no downtime)

1. Merge this PR (merge commit M). Then the pin PR: `hosts/studio.toml` `revision` = M, image tags
   `<M12>-arm64`; merge it (merge commit P). M is what the images are built from; P is what the deploy checkout
   is on, because only P carries the filled-in host profile (at M it still holds the `<placeholder>` pin and
   build/render refuse). `gbrainctl build` reads `revision` from the profile and builds M itself.
2. Create the 1P secure note `gbrain stack secrets v1` (Engineering vault) with four `KEY=base64` lines:
   * `TUNNEL_SSH_KEY_B64` a NEW ed25519 key for the rerank tunnel; authorize its public half on ACE-AI in
     `~ace/.ssh/authorized_keys` as `restrict,port-forwarding,permitopen="127.0.0.1:8084" ssh-ed25519 AAAA... gbrain-stack-tunnel`
   * `TUNNEL_KNOWN_HOSTS_B64` `ssh-keyscan -t ed25519 192.168.1.216 > /tmp/kh-ai`, then check it (below), then
     `base64 < /tmp/kh-ai`
   * `NAS_SSH_KEY_B64` the key restic uses for `truenas_admin@192.168.1.159` (today the Studio's `~/.ssh/id_ed25519`;
     a dedicated key authorized for that user is the better choice)
   * `NAS_KNOWN_HOSTS_B64` `ssh-keyscan -t ed25519 192.168.1.159 > /tmp/kh-nas`, then check it (below), then
     `base64 < /tmp/kh-nas`

   Check each scanned key before it goes into the note: a scan is unauthenticated, so compare its fingerprint with
   the one the host itself reports over the Studio's existing, already-trusted ssh (the connection the launchd
   tunnel and restic use today). The two lines of each pair must be identical; if not, stop:
   ```
   ssh-keygen -lf /tmp/kh-ai;  ssh ace@192.168.1.216 ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ssh-keygen -lf /tmp/kh-nas; ssh truenas_admin@192.168.1.159 ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   rm /tmp/kh-ai /tmp/kh-nas   # after the note is saved
   ```
3. On the Studio: a deploy checkout at P (the pin PR's merge commit, NOT M), then build + render (render fails
   naming the key if step 2 is incomplete):
   ```
   git clone --reference ~/.hermes/mirrors/ANG-Ventures/gbrain-source.git https://github.com/ANG-Ventures/gbrain-source.git ~/gbrain-stack-live
   git -C ~/gbrain-stack-live checkout --detach <P: the pin PR's merge sha>
   cd ~/gbrain-stack-live/deploy/stack && python3.14 ./gbrainctl build --host studio && python3.14 ./gbrainctl render --host studio
   ```

## Cutover (operator window: expect 15-30 s without gbrain)

The proof's stop -> up cycle took 15.5 s to `/health`. Production adds the shutdown checkpoint of the live
`gbrain-pg` (6 GB shared_buffers; the proof copy's took 0.7 s with a cold cache) and one render through
op-cached-read. Callers degrade, they do not fail: the mem0 prefetch's gbrain leg times out to `[]` for the turns in
that window, `rail.sh` falls back to the cold CLI (which needs postgres, so it fails for those seconds).

```
cd ~/gbrain-stack-live/deploy/stack
U=$(id -u)
# 1. stop the scheduled jobs that the cron container replaces (pause, never delete)
hermes cron pause 9b1a4fdaaa2a   # gbrain-nightly-sync, export + import, left UNCHANGED for the rollback
hermes cron resume <export-row>  # gbrain-observer-export, 03:25, export only (new row, shipped paused; see hermes-home edits)
hermes cron pause a3bcde3fa92e   # gbrain-pg-backup
hermes cron pause 7d0b9686f64e   # gbrain-deploy-parity
# 2. downtime starts: stop + disable the launchd jobs (plists stay in ~/Library/LaunchAgents)
for l in ai.gbrain.serve ai.gbrain.rerank-proxy ai.gbrain.rerank-tunnel; do launchctl bootout gui/$U/$l; launchctl disable gui/$U/$l; done
# 2b. the brain repo index is shared by host git and the cron container: compare mtime+size only (else each side
#     rehashes all 18 GB after the other wrote the index; measured in the proof)
git -C ~/gbrain/brain config core.checkStat minimal && git -C ~/gbrain/brain config core.trustctime false
# 3. keep the hand-run postgres container for rollback, under another name. Its restart policy goes to `no` FIRST
#    (it is unless-stopped, read 2026-11-21): else an OrbStack or Mac restart starts it again next to the compose
#    postgres on the same volume and port 5433. Readback must print `no`; anything else: stop here.
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' gbrain-pg            # unless-stopped (the rollback restores it)
docker update --restart=no gbrain-pg
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' gbrain-pg            # no
docker stop -t 60 gbrain-pg && docker rename gbrain-pg gbrain-pg-launchd-era
# 4. the stack
python3.14 ./gbrainctl up --host studio --allow-prod
until curl -sf -m 3 http://127.0.0.1:8199/health | grep -q '"engine":"postgres"'; do sleep 1; done   # downtime ends
# 5. checks
python3.14 ./gbrainctl status --host studio                                # STATUS=ok
curl -s http://127.0.0.1:18085/health                                      # primary_ok true
~/gbrain/rail.sh search "somfy blinds" --limit 3                           # warm path answers
docker exec -u 502:20 gbrain-cron-1 touch -d @$(stat -f %m ~/gbrain/.gbrain/last-nightly-sync) /state/last-nightly-sync
docker exec -u 502:20 gbrain-cron-1 gbrainjob run deploy-parity           # result=ok
docker exec -u 502:20 gbrain-cron-1 touch /state/ARMED && touch ~/.hermes/state/gbrain-stack/ARMED-AT
python3.14 ./gbrain-stack-watch --host studio; echo rc=$?                   # rc=0
# 6. only after a green night (03:30 nightly-sync + 04:15 pg-backup result lines ok):
docker rm gbrain-pg-launchd-era
```

## Rollback (to the launchd jobs)

Each step has its readback; do not start the next step until it prints what the comment says.
```
cd ~/gbrain-stack-live/deploy/stack; U=$(id -u)
# R1. silence the stack watch first, or it pages "service NOT RUNNING" every hour once the stack is down
hermes cron pause <gbrain-stack-watch row>
hermes cron list --all --json | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(j["id"], j.get("name"), j.get("enabled"), j.get("state")) for j in (d.get("jobs", d) if isinstance(d, dict) else d) if j.get("name") == "gbrain-stack-watch"]'   # enabled False / paused
# R2. the stack down
python3.14 ./gbrainctl down --host studio --allow-prod        # never `-v`: pgdata is external and stays
docker ps --filter label=com.docker.compose.project=gbrain -q | wc -l   # 0
# R3. the launchd-era postgres back, with its restart policy restored
docker rename gbrain-pg-launchd-era gbrain-pg && docker update --restart=unless-stopped gbrain-pg && docker start gbrain-pg
docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' gbrain-pg            # unless-stopped
until docker exec gbrain-pg pg_isready -q; do sleep 1; done; nc -z 127.0.0.1 5433 && echo pg-5433-ok   # pg-5433-ok
# R4. the launchd jobs
for l in ai.gbrain.rerank-tunnel ai.gbrain.rerank-proxy ai.gbrain.serve; do launchctl enable gui/$U/$l; launchctl bootstrap gui/$U ~/Library/LaunchAgents/$l.plist; done
for l in ai.gbrain.rerank-tunnel ai.gbrain.rerank-proxy ai.gbrain.serve; do launchctl print gui/$U/$l | grep -m1 'state ='; done   # state = running x3
until curl -sf -m 3 http://127.0.0.1:8199/health | grep -q '"engine":"postgres"'; do sleep 1; done; echo serve-ok   # serve-ok
curl -s http://127.0.0.1:18085/health                                      # primary_ok true
# R5. the launchd-era cron rows
hermes cron pause <export-row>   # the export-only row; 9b1a4fdaaa2a below exports AND imports, as before
hermes cron resume 9b1a4fdaaa2a; hermes cron resume a3bcde3fa92e; hermes cron resume 7d0b9686f64e
hermes cron list --all --json | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(j["id"], j.get("name"), j.get("enabled"), j.get("state")) for j in (d.get("jobs", d) if isinstance(d, dict) else d) if j["id"] in ("9b1a4fdaaa2a", "a3bcde3fa92e", "7d0b9686f64e")]'   # 3 rows: True scheduled
```
The data volume is the same in both directions; nothing is migrated, so rollback loses nothing written meanwhile.
After step 6 of the cutover (old container removed), rollback recreates it with the old `docker run` line from
ANG-Ventures/gbrain `deploy/gbrain-pg/docker-compose.yml` (same image digest, volume, port).

## Edits that ride with the cutover (not in this PR)

hermes-home (one PR, merged before the window; every row edit is a pause or a new row, nothing deleted):

* `cron/jobs.json` 9b1a4fdaaa2a gbrain-nightly-sync: NOT edited. It keeps its export + import command, so the
  rollback only resumes it.
* `cron/jobs.json` new row gbrain-observer-export (the `<export-row>` above): 03:25, export only (the
  workflow-observer `gbrain-sync.py` export, which writes INTO the brain repo from hermes data and stays on the
  host); the import half is the cron container's 03:30 `nightly-sync`. Ships `enabled: false`, `state: paused`,
  `paused_reason: "t_ab2fcba3 cutover resumes it when 9b1a4fdaaa2a is paused"`, so the two never export the same
  night. Cutover: pause 9b1a4fdaaa2a, resume this row. Rollback: pause this row, resume 9b1a4fdaaa2a.
* `cron/jobs.json` a3bcde3fa92e gbrain-pg-backup and 7d0b9686f64e gbrain-deploy-parity: `enabled: false`,
  `paused_reason: "replaced by the gbrain stack cron container (t_ab2fcba3)"`.
* `cron/jobs.json` new row gbrain-stack-watch: hourly, `no_agent`, `deliver` #alerts, script
  `gbrain-stack-watch.sh` = `exec python3.14 ~/gbrain-stack-live/deploy/stack/gbrain-stack-watch --host studio`.
* `scripts/gbrain-deploy-parity.sh`: header note "retired by the gbrain stack; see gbrain-stack-watch" (its
  `pgrep src/cli.ts serve` and `~/gbrain/app` checks are false once the serve is a container).
* `scripts/gbrain-pg-backup.sh`, `scripts/gbrain-rerank-tunnel.sh`: header note "retired by the gbrain stack";
  kept for the rollback.
* `fleet/ports.json` 8199 / 18085 owner `compose:gbrain/server` / `compose:gbrain/rerank-proxy` (were
  `launchd:ai.gbrain.*`); 18084 is no longer a host port (the tunnel lives inside the stack network).
* `fleet/background-jobs.json` (+ `.md`): the three `ai.gbrain.*` launchd entries -> disabled (rollback);
  add the `gbrain` compose project.
* `fleet/studio-checkout-sync.json`: drop the `ai.gbrain.rerank-proxy` unit (the proxy code is in the image now).
* `fleet/orbstack-policy.json` (+ its lint): count project `gbrain` as prod (it is: 2.0 + 4.0 + 0.5 + 0.25 + 2.0
  = 8.75 cores of caps, 22.3 GiB of mem_limit), or the drill admission keeps refusing every non-prod starter.
* `config/backup-lanes.json` gbrain-pg-backup lane: `tool` = the stack cron job; the success stamp path is unchanged.
* `fleet/ups-nut-studio/winddown-manifest.json`: stop the `gbrain` compose project instead of the launchd labels.

gbrain-ops skill (prose route, hermes-skills PR):

* "GitHub landing lane": replace "`git merge --ff-only gh/master` in `~/gbrain/app` -> `launchctl kickstart -k
  gui/$(id -u)/ai.gbrain.serve`" with "pin PR (`deploy/stack/hosts/studio.toml` revision + image tags) ->
  `gbrainctl build --host studio` -> `gbrainctl up --host studio --allow-prod` -> `gbrainctl status`"; the
  `gbrain-deploy-parity` paragraph becomes the `gbrain-stack-watch` paragraph.
* "Layout & invariants": the serve runs from the `gbrain-server` image, not `~/gbrain/app`; `~/gbrain/app` stays
  only as the launcher CLI's tree (cold CLI ops), and is no longer what production serves.
* "Postgres reality": the container is the `postgres` service of compose project `gbrain` (still named
  `gbrain-pg`, same volume, same port); recreate it with `gbrainctl compose --host studio --allow-prod -- up -d
  --force-recreate postgres`, not a hand `docker run`.
* "Nightly sync" / "Nightly pg_dump backup": the jobs run in the `gbrain-cron-1` container
  (`docker exec -u 502:20 gbrain-cron-1 gbrainjob show|run <job>`); logs in `/state` of that container; the
  stamp/skip counter moved from `~/gbrain/.gbrain/` to `/state`.
* "Reranker": the proxy and tunnel are the `rerank-proxy` / `rerank-tunnel` services; the tunnel uses its own
  restricted key (1P `gbrain stack secrets v1`); the proxy's fallback page now comes from `gbrain-stack-watch`.
* The ProcessType=Interactive and `launchctl bootstrap is blocked` notes become rollback-only.
