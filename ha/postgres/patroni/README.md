# PostgreSQL with Patroni (automatic failover, etcd DCS)

## Architecture

```text
            clients (try every node, use the writable one)
                 │
   ┌─────────────┼──────────────────────────┐
   ▼             ▼                          ▼
 node A        node B                     node C
 Patroni ─┐    Patroni ─┐                 Patroni ─┐      REST API :18008 (TLS)
 PostgreSQL│   PostgreSQL│ ◀── WAL ──     PostgreSQL│     :15432, leader + streaming standbys
 etcd ─────┴── etcd ─────┴──── raft ───── etcd ─────┘     client :12379, peer :12380 (TLS, client certs)
```

- Every node runs three lab services: `halab-etcd.service` (one member of a 3+ node etcd cluster), `halab-patroni.service` (Patroni, which starts and stops PostgreSQL itself) and the lab's halab cluster (`/var/lib/postgresql/<version>/halab`, configuration in `/etc/postgresql/<version>/halab`).
- etcd is the DCS (distributed configuration store). It holds the leader key, the member list and the cluster's dynamic configuration under `/halab/<scope>/`. Patroni uses the etcd v3 API (`etcd3:`).
- The leader holds a key with a TTL (`ttl`, default 30 s) and renews it every `loop_wait` (10 s). When the key expires, the healthiest replica takes it and promotes; the others follow the new timeline. Only the node holding the key may run as a primary, which is how Patroni avoids split brain.
- Replicas stream from the leader over TLS with the `repl` account, through a physical replication slot per member (`use_slots`).
- A failed old leader rejoins by itself: Patroni runs `pg_rewind` (account `rewind`) when its timeline diverged.
- `postgresql@<version>-halab.service` is masked and the cluster's `start.conf` is `manual`, so systemd never starts a second postmaster on the same data. `make destroy` reverses both.

### etcd

- Official release tarball (pinned 3.6.16, SHA-256 checked, x86_64 and aarch64) in `/opt/halab-etcd-v<version>`, not the distro package: installing Ubuntu's `etcd-server` starts an `etcd.service` on the standard ports 2379/2380, which could clash with an existing etcd.
- Data in `/var/lib/halab-etcd`, configuration and certificates in `/etc/halab/etcd`, user `halab-etcd`.
- TLS on both the client and the peer port, with client certificate authentication. A lab CA is created on the first node and copied to the others; each node gets its own certificate (SAN = node address). Patroni uses the same certificate for etcd and for its REST API. Mutual TLS is always on, also on Tailscale and VPN networks.
- `halab-etcdctl` (in `/usr/local/sbin`) wraps `etcdctl` with the endpoints and certificates: `halab-etcdctl endpoint health --cluster`, `halab-etcdctl member list -w table`.
- etcd needs a majority: 3 nodes survive the loss of 1, 5 survive 2. An even node count adds no tolerance.

### Patroni

- Patroni comes from PGDG (`patroni` pinned to apt.postgresql.org in `/etc/apt/preferences.d/halab-patroni`; Ubuntu 24.04's 3.2 predates PostgreSQL 17 support). Configuration: `/etc/halab/patroni/patroni.yml`. `halab-patronictl` wraps `patronictl -c` that file.
- Patroni keeps the Debian layout: it writes its own `postgresql.conf` in the data directory and includes `/etc/postgresql/<version>/halab/postgresql.conf` (`custom_conf`), so `conf.d/zz-ha-lab.conf`, `pg_hba.conf` and the TLS settings from provisioning still apply. Settings Patroni passes on the command line (`max_connections`, `max_wal_senders`, `max_replication_slots`, `wal_level`, `hot_standby`, `wal_log_hints`, `wal_keep_size`) come from the DCS; set them under `postgres.config` and `make ha` applies them cluster-wide.
- Patroni forces `cluster_name` to the scope, so the node name for the benchmark's `served_by` comes from the custom setting `halab.node`.
- REST API on port 18008 over TLS. Read endpoints (`/patroni`, `/cluster`, `/primary`, `/replica`, `/health`) need no password; changes (`/switchover`, `/config`, `/restart`, `/reinit`) need user `patroni` with `db_admin_password`.

## Settings

`config/ha.yml`:

```yaml
db_engine: postgres
ha_provider: patroni
patroni:
  scope: halab                     # Patroni cluster name (etcd prefix /halab/<scope>/)
  ttl: 30                          # leader key lifetime: the longest a dead leader blocks failover
  loop_wait: 10
  retry_timeout: 10                # DCS and PostgreSQL retry budget before the leader demotes itself
  maximum_lag_on_failover: 1048576 # bytes; a replica further behind is not promoted
  synchronous_mode: false
  synchronous_node_count: 1
  use_pg_rewind: true
  # rest_port: 18008  etcd_client_port: 12379  etcd_peer_port: 12380  etcd_version: "3.6.16"
```

`ttl`, `loop_wait`, `retry_timeout`, `maximum_lag_on_failover`, `synchronous_mode` and the PostgreSQL parameters are dynamic: `make ha` compares them with the DCS (`GET /config`), sends a `PATCH /config` when they differ, and restarts members whose change needs it (`patronictl restart --pending`). Keep `loop_wait + 2 * retry_timeout <= ttl`.

## Durability

| Mode | Commit acknowledged when | Loss on leader failure |
|---|---|---|
| `synchronous_mode: false` (default) | the leader flushed its WAL | WAL no replica received; Patroni refuses to promote a replica more than `maximum_lag_on_failover` bytes behind, so loss is bounded by that, but not zero |
| `synchronous_mode: true` | the synchronous standby flushed it | none: Patroni promotes only the synchronous standby. With no synchronous standby available, commits wait and no automatic failover happens |

`synchronous_mode` makes Patroni manage `synchronous_standby_names` (`synchronous_node_count` standbys). It does not use the `sync_replication:` section of the `sync-replication` strategy.

## Deploy

```bash
# config/ha.yml: db_engine: postgres, ha_provider: patroni (3 nodes, odd count)
# config/secrets.yml: db_replication_password and db_admin_password (>= 16 chars each)
make provision
make ha HA=patroni
make topology
```

First deploy:

1. etcd starts on every node and forms the cluster.
2. The replication role builds streaming replication from the node with `role: primary`, with the same rules as `streaming-replication`: a node holding data beyond the empty benchmark database is refused unless `ARGS="-e postgres_rebuild_replica=true"`. Data the lab did not create is never replaced.
3. Each node's halab cluster is stopped cleanly and handed to Patroni, the primary first. Patroni takes over the existing data: the primary starts as a replica, takes the leader key and promotes (timeline + 1); the standbys follow it. No data is copied again.
4. The dynamic configuration is applied and the role waits for one running leader and every other member streaming.

Re-running `make ha` changes nothing on a healthy cluster. It follows the current leader, not the node with `role: primary`. A node Patroni does not run yet (an interrupted deploy) is first made a standby of the leader with the same checks, then handed over. Adding a node to a running cluster is not supported (etcd's member list is fixed at bootstrap); destroy and redeploy.

## Health checks

`make topology` passes when:
- every node is up, exactly one is writable and every other one streams from it (same check as `streaming-replication`);
- Patroni reports one `primary` in state `running` and every other member `replica` with `replication_state: streaming`, no pending restart and the cluster not paused;
- with `synchronous_mode`, the leader has at least `synchronous_node_count` synchronous standbys;
- every etcd member is healthy.

It also prints `patronictl list`.

## Planned switchover

```bash
make switchover NEW_PRIMARY=db-02     # or without NEW_PRIMARY: Patroni picks a healthy member
```

The playbook sends `POST /switchover` to the leader's REST API. Patroni checks the candidate (healthy, streaming, within `maximum_lag_on_failover`; with `synchronous_mode` it must be the synchronous standby, otherwise HTTP 412 "candidate name does not match with sync_standby"), demotes the leader cleanly, promotes the candidate and restarts the old leader as a replica. The playbook waits until the old leader streams again. Writes pause for about 1-3 s. No acknowledged transaction is lost.

## Failover (leader lost)

Nothing to run: Patroni fails over by itself. `make promote` and the benchmark's `failover-test` do not promote for this strategy (`failover: patroni` in the descriptor); `make promote` only waits `auto_failover_wait` (45 s, descriptor) and records the writable node.

How long it takes:

| Failure | What Patroni does | Write outage observed in containers |
|---|---|---|
| `halab-patroni` stopped (`make fault FAULT=stop-db`) | clean shutdown, leader key released, a replica promotes at once | about 1.5 s, no loss |
| Leader hung or cut off (container paused) | key expires after `ttl`; the replica with the highest LSN promotes | about 30 s |
| PostgreSQL killed (`FAULT=kill-db`) | Patroni still holds the key and restarts PostgreSQL in place; no failover | seconds (crash recovery) |

With `synchronous_mode: false` a paused leader lost no acknowledged rows in the container test (its WAL had reached the replicas), but when it resumed it accepted 2 more commits for about 0.1 s before Patroni noticed the lost key and demoted it; `pg_rewind` then discarded them. That window exists because no watchdog is configured (see limitations). With `synchronous_mode: true` the same test lost nothing: commits on the resumed node cannot be acknowledged because no standby follows it.

## Recovery (old leader returns)

Usually automatic: when `halab-patroni` starts on the old leader, Patroni finds another leader, rewinds the node with `pg_rewind` if its timeline diverged, and starts it as a replica.

```bash
make recover NODE=db-01                          # starts halab-patroni and waits until the node streams
make recover NODE=db-01 ARGS="-e postgres_rebuild=true"   # patronictl reinit: replace its data with a copy of the leader
```

`postgres_rebuild=true` deletes the node's data directory and copies the leader (`pg_basebackup`). Use it only when the rewind fails (`journalctl -u halab-patroni` shows why).

If no node was writable when `make recover` ran (for example the only eligible node was the one being recovered), the rejoin step is skipped and the final health check may fail while Patroni elects; run `make topology` again a few seconds later.

## etcd quorum loss

Patroni needs the DCS to keep the leader key. Observed with `halab-etcd` stopped on 2 of 3 nodes:

1. The leader keeps serving writes until `retry_timeout`/`ttl` run out (about 25 s in the test).
2. It cannot renew the key and demotes itself ("demoting self because DCS is not accessible and I was a leader"). Every node is now a read-only replica; clients get `cannot execute INSERT in a read-only transaction`.
3. No acknowledged row was lost.
4. As soon as etcd has a majority again, the former leader takes the key back (within about 1 s in the test) and writes resume.

Losing one etcd member changes nothing for the database. Patroni's `failsafe_mode` (keep the leader while all members are reachable over the REST API) is not enabled.

## Monitoring

- The collector and `postgres_exporter` read PostgreSQL as for the other strategies: `ha_lab_writable`, `ha_lab_replication_position`, `replication_lag_seconds`, `ha_lab_sync_standbys`, `ha_lab_postgres_timeline` (it increases on every switchover and failover).
- `make topology` prints `patronictl list` (role, state, timeline, lag per member).
- Patroni and etcd log to the journal: `journalctl -u halab-patroni`, `journalctl -u halab-etcd`.
- `GET https://<node>:18008/primary` returns 200 only on the leader and can serve as a health check for a load balancer. The lab adds no HAProxy; the benchmark's `ha` endpoint tries every node and uses the writable one.

## Destroy

- `make destroy`: stops Patroni (replicas first, so it does not fail over) and etcd, removes their units, configuration, certificates and the etcd data (the DCS state belongs to this deployment), unmasks `postgresql@<version>-halab.service`, sets `start.conf` back to `auto`, starts the cluster from `/etc/postgresql`, promotes the former replicas and drops Patroni's replication slots. Every node ends standalone and writable with its data.
- `make destroy DATA=1`: also drops the halab cluster and purges Patroni (only if the lab installed it) and the PostgreSQL packages (only when no other cluster is left). Other clusters on the host are not touched.

## Known limitations

- No watchdog (`watchdog: mode off`). A leader that freezes and resumes can accept commits for up to one Patroni loop before it demotes itself (observed: 2 rows, then rewound away). Use `synchronous_mode` when that matters, or configure the Linux softdog outside the lab.
- With `synchronous_mode`, a leader crash followed by a stop within a few seconds can leave no eligible synchronous standby: Patroni clears the synchronous set while it restarts a crashed leader, so no replica promotes until the old leader returns (observed after `kill-db` then `stop-db`). This trades availability for no data loss.
- etcd membership is fixed at bootstrap: replacing or adding a node needs `make destroy` and a new deploy.
- `failsafe_mode`, standby clusters, Citus and tags (`nofailover`, `nosync`) are not configured from `config/ha.yml`.
- The lab CA key is copied to every node (each node signs its own certificate). Certificates are valid for 10 years and are not rotated.
- No HAProxy or virtual IP: clients must try every node, as the benchmark does.
- Verified in Ubuntu 24.04 systemd containers (arm64, PostgreSQL 17, Patroni 4.1.5, etcd 3.6.16) with the Ansible Docker connection; the nftables firewall and the local-to-node checks were not part of that test.
