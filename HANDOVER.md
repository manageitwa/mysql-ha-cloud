# Handover: Consul majority-loss recovery (H6, stage 3)

- **Date:** 30 October 2026
- **Base commit:** `4e2853c` (*fix: Do not bootstrap from the snapshot while other nodes are running*)
- **Status:** the code is written and uncommitted. Tests A–C pass. **Tests D, E and F haven't been run** against the final code.
- This file and `SECURITY-REPORT.md` are working documents and are not to be committed.

## 1. What the change does

When a majority of the Consul servers are lost at once (several containers killed or a host lost), Consul can't elect a leader. Before this change, the cluster either stayed stuck, or bootstrapped a separate cluster and lost data. Stage 2 (`4e2853c`) prevents the data loss. This stage makes the cluster recover by itself.

| Area | Change | File |
|---|---|---|
| Consul bootstrap | A new node only passes `-bootstrap-expect` when **no other task is running MySQL** (a cold start). Otherwise it only joins, so a separate Consul cluster can never be bootstrapped while other nodes are running. | `actions.py` `join_or_bootstrap`, `consul.py` `agent_start(bootstrap_expect=None)` |
| Recovery | A thread on every node checks every 10 s. A node recovers only when **all** of these hold: Consul has had no leader for **60 s**; some raft peers are **no longer Swarm tasks** (not in `tasks.<service>` DNS); the remaining peers can't form a majority; at least one replacement task exists; and this node is one of the remaining peers. It then stops its agent, **wipes the Consul data directory**, restarts with `-bootstrap-expect <number of remaining peers>`, and re-registers. Recovery can run at most once every 5 minutes. | `actions.py` `recover_consul_if_needed`, `start_consul_recovery_thread`; `consul.py` `agent_restart`, `has_leader`, `get_raft_peer_ips` (reads `/v1/operator/raft/configuration?stale`), `reset_session` |
| Process exit check | The main loop ignores the Consul process exiting while `Actions.consul_restarting` is set | `actions.py` |
| MySQL failover | A node that **isn't replicating** (a former leader) publishes the transactions it wrote itself (filtered by its own `server_uuid`) instead of an empty set, so it can win failover again with no loss | `mysql.py` `get_leader_gtids` |
| Leader lock safety | `try_to_become_replication_leader` refuses to run without a session (it used to write the leader key **without a lock**). `reset_session` creates the new session before replacing the old one. | `consul.py` |
| Startup | `Consul.__init__` waits **indefinitely** for a session (`create_node_health_session(wait=True)`). Before, it crashed after about 60 s, so replacements kept crashing during a recovery. | `consul.py` |
| `server_id` | **Random** between 1 and 4294967295, excluding the IDs of running servers (read over 3306 with the replication user). The Consul counter (`mcm/server_id`, `get_mysql_server_id`) has been **removed**. | `mysql.py` `choose_server_id`, `get_other_server_ids`, `build_configuration` |
| Re-registration | `register_node` writes the known `server_id` and `mysql_version` instead of `""` | `consul.py` |

### Why `server_id`s are now random (tested)

A three-server chain was tested: A (`server_id` 7) → B (1) → C (7). C **silently skipped every event that A had written** (`CREATE DATABASE`, `CREATE TABLE`, `INSERT`), because MySQL ignores events stamped with its own `server_id`. After Consul loses its state, nothing records past IDs. A counter, or "highest running ID + 1", could hand out a dead leader's ID while its events were still being replicated. Random 32-bit IDs make reuse negligible.

## 2. Bugs found and fixed during this stage

1. **Leader key written without a lock.** `reset_session()` set the session to `None`, and failover on the main loop then called `kv.put(acquire=None)`. The key could never be released, and every node deadlocked with no writable leader.
2. **Replacements crashed during the recovery.** Session creation at startup gave up after about 60 s, which was about when the recovery happened. Replacements that crashed after joining the recovered cluster made it lose its majority again, which caused a loop.
3. **Duplicate `server_id`s** (two nodes with 3). Seeding from the registry raced with survivors re-registering. Superseded by random IDs.
4. **Recovery check inside the main loop.** The main loop retries Consul calls for up to 3 minutes while there's no leader, so the check was moved to its own thread.

## 3. Test environment

The tests run on the local single-node Swarm, from a **temporary copy** of `dev.docker-compose.yml`.
- **Absolute paths:** `./src` and similar are rewritten so the compose file works from `/tmp`.
- **A named volume replaces `./snapshots`.** Clearing `snapshots/current` (which is root-owned) with `rm` is blocked by Claude Code's safety check, so the test stack uses a named volume that `docker volume rm` can clean up.
- **The helper files are in `/tmp/mcm-rec/`**: `stack.yml` (`CONSUL_BOOTSTRAP_EXPECT: 2`), `stack3.yml` (the same with `3`), and `lib.sh`. If `/tmp` has been cleared, recreate them:

```bash
cd /var/home/ben/Projects/Manageit/mysql-ha-cloud
mkdir -p /tmp/mcm-rec
sed -e "s#\./src#$PWD/src#g" -e "s#\./entrypoint#$PWD/entrypoint#g" -e "s#\./cert#$PWD/cert#g" \
    -e 's#- \./snapshots:/snapshots#- mcmtestsnap:/snapshots#' dev.docker-compose.yml > /tmp/mcm-rec/stack.yml
printf 'volumes:\n  mcmtestsnap:\n' >> /tmp/mcm-rec/stack.yml
sed 's/CONSUL_BOOTSTRAP_EXPECT: 2/CONSUL_BOOTSTRAP_EXPECT: 3/' /tmp/mcm-rec/stack.yml > /tmp/mcm-rec/stack3.yml
```

`/tmp/mcm-rec/lib.sh`:

```bash
q() { docker exec $1 mysql -uroot -proot_pass -N -e "$2" 2>/dev/null; }
app() { docker exec $1 mysql -uapplication_user -papplication_pass -h127.0.0.1 -P6033 -N application -e "$2" 2>&1 | grep -v Warning | head -1 | cut -c1-90; }
n_ready() { n=0; for x in $(docker ps --filter name=mhtest_mysql -q); do docker logs $x 2>&1 | grep -q "Setting up replication\|new replication leader\|Register new service_id=.*leader" && n=$((n+1)); done; echo $n; }
wait_ready() { for i in $(seq 1 ${2:-60}); do [ $(n_ready) -ge ${1:-4} ] && return 0; sleep 5; done; return 1; }
roles() { L=""; R=(); for c in $(docker ps --filter name=mhtest_mysql -q); do [ "$(q $c 'SELECT @@read_only')" = "0" ] && L=$c || R+=($c); done; }
cs() { docker exec $1 bash -c 'export CONSUL_HTTP_TOKEN=$(python3.14 -c "import json;print(json.load(open(\"/tmp/consul/mcm.json\"))[\"acl\"][\"tokens\"][\"agent\"])"); '"$2" 2>&1; }
deploy() { docker stack rm mhtest >/dev/null 2>&1; until [ -z "$(docker ps -aq --filter name=mhtest_mysql)" ]; do sleep 2; done; until ! docker network ls | grep -q mhtest_mysql-ha; do sleep 2; done; docker volume rm mhtest_mcmtestsnap >/dev/null 2>&1; docker stack deploy -c "$1" mhtest >/dev/null 2>&1; }
state() { for c in $(docker ps --filter name=mhtest_mysql -q); do echo "   $c ro=$(q $c 'SELECT @@read_only') sid=$(q $c 'SELECT @@server_id') qt=$(q $c "SELECT COUNT(*) FROM application.qt" 2>/dev/null) repl=$(q $c "SELECT CONCAT(c.SERVICE_STATE,'/',a.SERVICE_STATE) FROM performance_schema.replication_connection_status c JOIN performance_schema.replication_applier_status a") health=$(docker inspect -f '{{.State.Health.Status}}' $c)"; done; }
```

**Build before each run.** The dev stack mounts `src/` from the working tree, but the Dockerfile still needs rebuilding if it changes:

```bash
python3 -m py_compile src/mcm/*.py && docker build -q -t manageitwa/mysql-ha-cloud:test .
```

**Keep waits bounded** (at most about 4 minutes, with progress output). An unbounded polling loop ran for over 7 minutes during test C.

## 4. Test results

| Test | Scenario | Result |
|---|---|---|
| **A** | Fresh start, 4 nodes | ✅ Ready in about 36 s. All nodes used `-bootstrap-expect` (cold start), one Consul cluster formed. |
| **B** | 4 nodes, `CONSUL_BOOTSTRAP_EXPECT: 2`. Write `application.qt` after the snapshot, then `docker kill` 2 replicas at once. | ✅ (final run, random IDs) A writable leader returned after about **114 s**, with no container crashes. `qt` is on all 4 nodes, replication is `ON/ON`, and all 4 `server_id`s are distinct. The **old leader was re-promoted**, which exercises the `get_leader_gtids` change. Earlier run: about 86 s, and writes through ProxySQL replicated to all 4 nodes. |
| **C** | As B, with `CONSUL_BOOTSTRAP_EXPECT: 3` (like production, where Consul used to stay stuck) | ✅ (run before random IDs) Recovered in about 94 s with no crashes, and data intact. Not re-run since the switch to random `server_id`s. |
| **D** | Kill **1** replica | ⏳ **Not run.** Expected: Swarm replaces it, **no recovery is logged** (`lost the majority`), and the leader keeps writing. |
| **E** | Rolling update: `docker service update --env-add ROLL=1 --detach=false mhtest_mysql` (`start-first`) | ⏳ **Not run.** Expected: the update converges, new tasks start **without** `-bootstrap-expect` (other MySQL running) and join, no recoveries, and data intact on all nodes. |
| **F** | Cold boot: `docker service scale mhtest_mysql=0`, wait until all are gone, then `=4` | ⏳ **Not run.** Expected: all 4 nodes use `-bootstrap-expect` and it bootstraps from the snapshot as before (writes since the snapshot are lost, by design). |

Also worth re-running: **C on the final code** (`deploy /tmp/mcm-rec/stack3.yml`).

### Test B/C procedure (for re-runs)

```bash
. /tmp/mcm-rec/lib.sh; deploy /tmp/mcm-rec/stack.yml   # or stack3.yml for test C
wait_ready 4 30; sleep 10; roles
q $L "CREATE TABLE application.qt (id INT AUTO_INCREMENT PRIMARY KEY, v VARCHAR(30)); INSERT INTO application.qt (v) VALUES ('after snapshot')"; sleep 2
docker kill ${R[0]} ${R[1]}; T=$(date +%s); W=""
while [ $(( $(date +%s) - T )) -lt 240 ]; do sleep 10; w=""
  for c in $(docker ps --filter name=mhtest_mysql -q); do [ "$(q $c 'SELECT @@read_only')" = "0" ] && w=$c; done
  [ -n "$w" ] && [ $(n_ready) -ge 4 ] && { W=$w; break; }; done
echo "writable leader ${W:-NONE} after ~$(( $(date +%s) - T ))s; crashed: $(docker ps -a --filter name=mhtest_mysql --filter exited=1 -q | wc -l)"
sleep 10; state
```

**Pass criteria:**
- a writable leader within 4 minutes
- no containers that exited with code 1
- `qt=1` on every node
- replicas `ON/ON`
- 4 distinct `server_id`s
- 0 `Traceback` in the logs

**Useful log markers:**
- `Consul has lost the majority of its servers`
- `Restarting the Consul agent with its data discarded`
- `We are the new replication leader`
- `MySQL is running on other nodes`
- `No Consul session, unable to become replication leader`

## 5. Current state (at handover)

- **The Docker test resources have been removed:** the `mhtest` and `mrpn` stacks, their containers and networks, and the `mhtest_mcmtestsnap` volume. To continue with test D, deploy a fresh stack first (`. /tmp/mcm-rec/lib.sh; deploy /tmp/mcm-rec/stack.yml; wait_ready 4 30`).
- **The helper files in `/tmp/mcm-rec/` have been kept.** Recreate them using section 3 if they're gone.
- **`manageitwa/mysql-ha-cloud:test`** was last built from the final code, so rebuild only if the code changes.
- **Cleanup after further tests:**

```bash
docker stack rm mhtest; until [ -z "$(docker ps -aq --filter name=mhtest_mysql)" ]; do sleep 2; done
until ! docker network ls | grep -q mhtest_mysql-ha; do sleep 2; done
docker volume rm mhtest_mcmtestsnap; rm -rf /tmp/mcm-rec
```

## 6. Open points before committing

1. **Run tests D, E and F**, and re-run C on the final code.
2. **Network partition: untested.** It can't be reproduced on a single-host Swarm. The design depends on Swarm's view: during a partition, unreachable servers should still be listed as tasks, so recovery shouldn't run. **Residual risk:** if a partition leaves Swarm's managers on the side *without* the Consul majority, Swarm may reschedule the other side's tasks, and that side could recover into a second cluster. This needs documenting.
3. **Timings:** the write outage after losing the majority is about 80–115 s, mostly the 60 s `consul_recover_after` wait. Consider whether to shorten it.
4. **Log noise:** a node that can't recover logs *"Consul has had no leader … cannot be recovered automatically"* every 10 s, and the guard logs every 5 s. Consider rate-limiting.
5. **README:** document automatic recovery, when it does and doesn't run, the partition caveat, and that `server_id`s are now random. Decide whether recovery should be switchable with an environment variable (not implemented; it's always on).
6. **`SECURITY-REPORT.md`:** update H6 once this is committed. L27 (the `server_id` counter could be rolled back) no longer applies, because the counter has been removed.
7. **Suggested commit message:**

```
feat: Recover automatically when Consul loses the majority of its servers

When most Consul servers are lost at once, the remaining servers cannot
elect a leader and the lost servers never return, as Consul's data is not
kept. The remaining servers now discard their Consul data and bootstrap a
new Consul cluster once Swarm has replaced the lost servers, and the MySQL
leader is decided again by failover. A former leader now publishes its own
transactions, so it is promoted again if it has the most recent data.

New nodes only bootstrap Consul in a cold start of the whole cluster, so a
separate Consul cluster is never created while other nodes are running.
Nodes wait for Consul indefinitely while starting rather than exiting.

MySQL server IDs are now random rather than counted in Consul, as a replica
ignores transactions written with its own server ID and Consul's count is
lost with its state.
```
