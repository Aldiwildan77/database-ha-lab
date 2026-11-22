# Database engines and HA strategies

The lab core is engine-neutral. Everything specific to one database lives in that engine's folder, in its JMeter dialect and in its metrics collector. MySQL is ready. MariaDB is ready for `async-replication`, `semi-sync` and `galera`, and PostgreSQL for `streaming-replication`, `sync-replication` and `patroni`. MariaDB's `maxscale` is planned. The runbook for each engine and strategy is in [ha/README.md](../../ha/README.md).

## Layout

```text
ansible/
  engines/
    mysql/                 ready
      engine.yml           descriptor: ports, packages, exporter, collector, HA strategies
      roles/               mysql, mysql-exporter, replication, orchestrator, innodb-cluster,
                           percona-xtradb-cluster, percona-repo
      tasks/               hooks called by the core playbooks (see "Engine hooks")
    mariadb/               ready (async-replication, semi-sync, galera)
      engine.yml           descriptor; maxscale is status: planned
      roles/               mariadb, mariadb-replication, mariadb-galera (reuse mysql-exporter)
      tasks/               hooks
    postgres/              ready (streaming-replication, sync-replication, patroni)
      engine.yml           descriptor
      roles/               postgres, postgres-replication, postgres-exporter, etcd, patroni
      tasks/               hooks
  roles/                   core roles: common (firewall), node-exporter, lab-collector
  playbooks/, tasks/       core: engine-neutral, call the engine's roles and hooks
jmeter/
  scripts/dialects/<engine>.groovy    SQL differences for the benchmark
  drivers/<engine>.env                JDBC driver (Maven coordinates)
metrics/collectors/
  ha_lab_collector.py                 loader
  engine_<engine>.py                  queries the database, emits the neutral metrics
local/prometheus/rules/engine-<engine>.yml   engine-only recording rules
```

`ansible.cfg` puts every engine's `roles/` folder on `roles_path`.

## Selection

`config/ha.yml`:

```yaml
db_engine: mysql             # default
ha_provider: async-replication
```

The inventory plugin loads every `ansible/engines/*/engine.yml` and checks each one at load time (unknown port names, missing keys). It rejects an unknown engine, a strategy the engine does not support, an engine with `status: planned` and a strategy with `status: planned`.

## Descriptor

| Key | Purpose |
|---|---|
| `name`, `display`, `status` | `status: planned` blocks selection until the roles exist; the same key on one `ha.<strategy>` blocks only that strategy |
| `config_section`, `versions`, `default_version` | Engine settings section in `ha.yml` (`mysql:`, `mariadb:`, `postgres:`); the same key under a node holds per-node server options |
| `architectures` | CPU architectures `make validate` accepts (`ansible_architecture` values); default `[x86_64]` |
| `service`, `process_names`, `helper_processes`, `marker_dir`, `config_file` | Used by host checks, faults and markers; `config_file` is the lab settings file |
| `paths_written` | Paths provisioning writes, shown by `make plan-host` |
| `packages`, `conflicting_packages`, `apt_repository` | Per strategy or `default`; used by `make plan-host` |
| `ports`, `primary_port`, `exporter_port` | Lab defaults that differ from the product standard; `config` names the `ha.yml` override; `span` reserves consecutive ports |
| `exporter` | Role that installs it, its name, and its "up" metric |
| `service_unit` | systemd unit that runs the server (`{version}` is replaced with `db_version`); the firewall starts before it, the collector after it, and `fault.yml` / `recover.yml` stop and start it. Default `<service>.service`; `ha.<strategy>.service_unit` overrides it when an HA manager runs the server (Patroni) |
| `collector` | Collector module, the system user it runs as, and how it connects |
| `ansible_collections` | Collections this machine needs for the engine (checked by `make validate-local`) |
| `report_queries` | Optional engine-only PromQL added to the report |
| `ha.<strategy>` | `family`, `failover`, traits (`semi_sync`, `synchronous`), node limits, mesh and client ports, `ha_endpoint`, `service_unit`, `auto_failover_wait` (seconds `make promote` waits for an automatic failover; default 5), and the roles for `provision` and `deploy` |

Strategy traits drive the core logic:

- `family`: `replication`, `group` or `galera`.
- `failover`: `manual` (the lab promotes with `make failover`) or the component that promotes on its own (`orchestrator`, `group`, `galera`, `maxscale`, `patroni`).
- `ha_endpoint`: the proxy the benchmark's `ha` endpoint uses (MySQL Router, MaxScale). Without it, the client tries every node and picks the writable one.

## Engine hooks

Core playbooks call these task files in `ansible/engines/<engine>/tasks/`. Each is a plain task list.

| Hook | Called by | Contract |
|---|---|---|
| `topology-facts.yml` | every topology read | Sets `lab_topo`: `node`, `db_up`, `error`, `read_only`, `position`, `replica` (`source_host`, `io_state`, `sql_state`, ...), `group`, `status` |
| `ha-health.yml` | `topology.yml` | Fails with a readable reason when the topology is unhealthy; gets `lab_topo_all`, `lab_writable`, `lab_down` |
| `failover-choose.yml` | `failover.yml` (manual) | Fences the old primary and sets `fo_new_primary`, or fails when no candidate is safe |
| `failover-promote.yml` | `failover.yml` (manual) | Makes `fo_new_primary` writable |
| `failover-unfence.yml` | `failover.yml` rescue | Undoes the fence when no candidate was chosen |
| `failover-repoint.yml` | `failover.yml` (manual) | Points the other nodes at `fo_new_primary` |
| `switchover-managed.yml` | `failover.yml` (HA manager) | Planned switchover through the HA manager |
| `recover-rejoin.yml` | `recover.yml` | Rejoins `node` to the topology led by `rc_primary` |
| `destroy-teardown.yml` | `destroy-ha.yml` | Removes the HA layer of `destroy_provider` |
| `destroy-purge.yml` | `destroy-ha.yml` with `DATA=1` | Stops the server, purges packages and data |
| `server-version.yml` | `collect-info.yml`, `benchmark-context.yml` | Sets `lab_db_version` |
| `node-summary.yml` | `provision.yml` | Sets `lab_db_summary` |
| `mesh-login.yml` | `check-mesh.yml` | TLS login to every peer; registers `mesh_login` |
| `ledger-dump.yml` | `consistency.yml` | Writes the run's txn_ids to `ledger_file`; `cs_dump.stdout` is `<position> <rows> <balance sum>` |

## Neutral names

- Inventory group: `db_nodes`. `mysql_nodes` remains as an alias.
- Variables: `db_engine`, `db_version`, `db_port`, `db_exporter_port`, `db_config`, `db_node_config`, `lab_engine`, `lab_ha_strategy`, `lab_ha_endpoint`, `lab_engine_tasks`. MySQL roles keep their own `mysql_*` variables; PostgreSQL roles and hooks share the `pg_*` names in `ansible/inventory/group_vars/all/postgres.yml`.
- Secrets: `db_exporter_password`, `db_replication_password`, `db_benchmark_password`, `db_admin_password` (env `LAB_DB_*_PASSWORD`). The older `mysql_*` keys and `LAB_MYSQL_*` env vars are still read.
- Metrics: `ha_lab_db_up`, `ha_lab_writable`, `ha_lab_read_only`, `ha_lab_replication_position`; the Prometheus job is `db`; the availability alert is `DatabaseDown`.
- Faults: `stop-db`, `kill-db` (`stop-mysql` and `kill-mysql` are aliases).
- The benchmark context records `db_engine` and `db_version`; the report's Database column shows both.

## Adding an engine

1. Write `ansible/engines/<engine>/engine.yml` (copy the MySQL one) with `status: planned`. Add `ansible.collections` the roles need to `requirements.yml` (pinned).
2. Add the roles under `ansible/engines/<engine>/roles/` and every hook in `tasks/`.
3. Add `metrics/collectors/engine_<engine>.py` (class `Collector(cfg)` with `.lock` and `.collect(registry)` that emits the neutral metrics).
4. Add `jmeter/scripts/dialects/<engine>.groovy` and `jmeter/drivers/<engine>.env`.
5. Add engine-only rules in `local/prometheus/rules/engine-<engine>.yml` if needed.
6. Run the container tests for each strategy, then set `status: ready` (per strategy while others are still `planned`).

## MariaDB

Target: MariaDB 11.4 LTS on Ubuntu 24.04, installed from `deb.mariadb.org` and pinned to `mariadb-*` packages only. The provisioning role refuses a datadir it did not create, writes only `/etc/mysql/mariadb.conf.d/zz-ha-lab.cnf`, and uses the lab port (13306).

How it differs from MySQL:

- GTIDs are `domain-server-seq`. The position is `gtid_current_pos`; `gtid_strict_mode` is on, so a replica that executed extra transactions is refused by the primary (error 1236) and by the role before it connects.
- Replicas start with `MASTER_USE_GTID=current_pos`, not `slave_pos`. A node that was primary has its own writes in the binlog and an empty or stale `gtid_slave_pos`; `slave_pos` would replay history on it.
- There is no `super_read_only` and no `SET PERSIST`. `read_only` gates writes (root bypasses it through `READ ONLY ADMIN`); the roles write `read_only` and the semi-sync flags to `/etc/mysql/mariadb.conf.d/zz-ha-lab-role.cnf` so they survive a restart.
- Semi-sync is built in (`rpl_semi_sync_master_enabled`, `rpl_semi_sync_slave_enabled`). It always waits for one replica, so `semi_sync.wait_for_replica_count` must be 1.
- Replica state comes from `SHOW SLAVE STATUS`. There are no Performance Schema replication tables, so the collector has no per-transaction apply histogram and no per-COMMIT histogram. Server-side statement time comes from the `query_response_time` plugin.
- Promotion safety: the hook waits until each candidate's `gtid_current_pos` reaches the old primary's (or the last position the replica received), then picks a candidate that is at or ahead of every other candidate in every domain. When none is, it refuses. `MASTER_GTID_WAIT` is not used: it follows `gtid_slave_pos`, which stays behind on a node that was primary earlier.
- The shared `mysql-exporter` role is reused. `ansible.mysql` 5.x warns that its MariaDB support ends in 6.0.0; `requirements.yml` pins 5.2.1.

Galera (`ha_provider: galera`, role `mariadb-galera`, runbook [ha/mariadb/galera](../../ha/mariadb/galera/README.md)):

- Provisioning is the same as for the replication strategies (standalone `mariadb` role). `make ha` installs `galera-4`, `mariadb-backup` and `socat`, writes `/etc/mysql/mariadb.conf.d/zz-ha-lab-galera.cnf`, bootstraps the role-primary node with `galera_new_cluster` when no node holds a primary component, and starts the others one at a time (SST with `mariabackup`). Unlike PXC, the packages do not change, so the cluster is formed by `make ha`, not by provisioning.
- A joiner's tables outside the system schemas would be replaced by the SST; the role refuses unless `-e mariadb_galera_rebuild_joiner=true`. When every node is down but `grastate.dat` shows the cluster ran before, `make ha` refuses and points to `make recover`.
- On private networks the role shares one self-signed certificate (`/etc/mysql/halab-galera-tls`) and turns on `socket.ssl` for group communication and IST, and `encrypt=4` (`VERIFY_CA`) for SST.
- `wsrep_gtid_mode=ON` with one `wsrep_gtid_domain_id`. Galera GTIDs keep the origin's `server_id`, so `gtid_current_pos` stays empty on the other nodes; the topology, ledger and collector read `gtid_binlog_pos` instead.
- `topology-facts` reports a node outside the primary component (`wsrep_ready=OFF`) as read-only. `ha-health` requires every node `Synced` in one primary component and says whether a degraded cluster still has quorum.
- `failover.yml` has nothing to promote: the switchover hook only reports that every node is writable.
- `recover-rejoin` (run on `node`, acts on peers): with a primary component, it (re)starts `node`, which rejoins through IST or SST; when nodes run but none is primary (quorum lost), it sets `pc.bootstrap` on the running node with the highest `wsrep_last_committed` and starts the rest; when every node is down, it reads `grastate.dat` (and runs `galera_recovery` where `seqno` is -1), bootstraps the highest seqno and starts the others. `recover.yml` does not start a Galera node itself (for PXC the MySQL hook starts it). `-e mariadb_galera_bootstrap_node=<n>` overrides the choice when a node cannot be read.
- `destroy-teardown` dissolves the cluster in place: every node keeps its data and restarts standalone.

## PostgreSQL

PostgreSQL 16, 17 (default) or 18 from the PGDG apt repository (`apt.postgresql.org`), pinned so only `postgresql*` packages come from it. The lab owns a cluster named `halab` (`pg_createcluster 17 halab --port 15432`, `/etc/postgresql/17/halab`) and never touches other clusters: it refuses a `halab` cluster it did not create (`-e postgres_adopt_existing=true` overrides), and `make destroy DATA=1` drops only `halab` and purges the packages only when no other cluster remains. The package's default `main` cluster is suppressed while the server package is installed.

Settings: `postgres.version`, `postgres.port`, `postgres.shared_buffers_ratio`, `postgres.config` (extra `postgresql.conf` options) and `sync_replication.num_sync` / `commit_level` in `config/ha.yml`; `postgres_exporter_port` under `monitoring`. Data checksums and `wal_log_hints` are on, so `pg_rewind` works.

| Strategy | Failover | Notes |
|---|---|---|
| streaming-replication | manual | asynchronous physical streaming; client picks the writable node (`pg_is_in_recovery()`), no `ha_endpoint` |
| sync-replication | manual | `synchronous_standby_names = 'ANY n (other nodes)'`, written per node, so any node can become the primary without reconfiguration |
| patroni | Patroni | etcd (official release, TLS with client certificates) on every node as the DCS; Patroni runs the halab cluster (`halab-patroni.service`, the cluster's own unit is masked) and fails over by itself. See [ha/postgres/patroni](../../ha/postgres/patroni/README.md) |

Accounts (created once on the primary, replicated): `exporter` (`pg_monitor`), `repl` (REPLICATION), `bench` (owns the benchmark database), `rewind` (password `db_admin_password`, execute on the file functions `pg_rewind` needs). Standbys authenticate through `~postgres/.pgpass`, so no password sits in `primary_conninfo`. TLS (`ssl=on`, `scram-sha-256`) is mandatory on private networks (`hostssl`).

Build (`make ha`): each node starts as its own cluster (provision). The deploy role copies the primary onto every replica with `pg_basebackup` (refused when the replica holds data other than the empty benchmark database, unless `-e postgres_rebuild_replica=true`), writes `standby.signal` and `primary_conninfo` (`application_name` = node name), and waits for streaming. Replaying the deploy changes nothing.

Failover (`failover.yml`, manual strategies), with LSNs instead of GTIDs:

1. Fence: the old primary is stopped cleanly (`pg_ctlcluster stop -m fast`). A clean stop makes the walsenders ship every WAL record, including the shutdown checkpoint, to the standbys. When the old primary is already down (unplanned) there is nothing to fence.
2. Wait until each candidate has replayed everything it received (and, after a clean stop, the shutdown checkpoint). A candidate that does not (cut off, lagging) is not eligible.
3. Choose the highest replay LSN. A requested `new_primary` that is not the highest is refused and the old primary is started again.
4. `pg_promote()`, `ALTER SYSTEM RESET primary_conninfo`, `CHECKPOINT`.
5. Repoint: running standbys get the new `primary_conninfo` and follow the new timeline (`recovery_target_timeline=latest`); the old primary, stopped cleanly, starts as a standby with `standby.signal` (no rewind is needed, and it refuses to start that way if it was not shut down cleanly).

Recover (`recover.yml -e node=<n>`): a node that returns as a standby is repointed; one that returns as a primary (the old primary after an unplanned failover) is stopped, rewound with `pg_rewind` (needs `db_admin_password`), and started as a standby. `-e postgres_rebuild=true` replaces its data with a base backup when it cannot be rewound. Known gap: `recover.yml` starts the service first, and the old primary comes up writable until the rejoin step stops it, so keep clients away from it until then.

With `sync-replication` and fewer reachable standbys than `num_sync`, commits wait. After an unplanned failover of a 2-node cluster the new primary has no standby to acknowledge, so writes block until the old primary returns.

Metrics: `engine_postgres.py` emits the neutral topology metrics (`ha_lab_writable` from `pg_is_in_recovery()` and `default_transaction_read_only`; `ha_lab_replication_position` is the WAL byte offset, insert LSN on a primary and replay LSN on a standby), `replication_lag_seconds`, `replication_queue_size{queue="wal_replay"}` in bytes, `ha_lab_sync_standbys`, transactions, deadlocks and lock waits. `postgres_exporter` (`pg_up`, port 19187) runs on every node. The dialect `jmeter/scripts/dialects/postgres.groovy` classifies errors by SQLSTATE; `served_by` is the node address, read from the custom setting `halab.node` (Patroni overrides `cluster_name`, which the other strategies also set to it).

Verified in containers (Debian trixie, PostgreSQL 17, Docker connection plugin, no systemd). Streaming replication: provisioning, deploy twice (the second changes nothing), topology, switchover, unplanned failover, recover with `pg_rewind` (WAL the old primary held and no standby had was removed), ledger dump, destroy with and without data. Sync replication: provisioning, deploy twice, topology (checks the synchronous standbys), switchover, a switchover refused because the requested node was cut off, unplanned failover, recover (rewind and `postgres_rebuild`), deploy refused over existing data and then `postgres_rebuild_replica`. Also run: the JMeter dialect (prepare and baseline, 0 errors), the collector module, and `postgres_exporter` (`pg_up 1`). Not run against real Ubuntu 24.04 hosts: the systemd units (exporter, collector), the apt and pin tasks on a host that has no PostgreSQL package yet, the firewall, `make plan-host` and `make validate`.

Patroni (`patroni`): the deploy builds streaming replication first (same refusal over existing data), then hands each node's halab cluster to Patroni, the primary first; Patroni takes over the existing data, so nothing is copied twice. The hooks read Patroni's REST API: `topology-facts` adds `status.patroni` (role, state, timeline, replication state), `ha-health` also checks Patroni roles and etcd member health, `switchover-managed` posts `/switchover`, `recover-rejoin` starts `halab-patroni` (and `patronictl reinit` with `postgres_rebuild=true`), `destroy-teardown` gives the cluster back to `postgresql@<version>-halab.service`. Verified in Ubuntu 24.04 systemd containers (arm64, PostgreSQL 17, Patroni 4.1.5 from PGDG, etcd 3.6.16): provisioning, deploy twice, re-provisioning under Patroni, topology, switchover (named, automatic, and refused for a non-synchronous candidate), failover after stopping Patroni and after pausing the leader (async and `synchronous_mode`, acknowledged rows checked), automatic `pg_rewind` of the old leader, `recover` with `postgres_rebuild`, etcd quorum loss and return, destroy with and without data next to a foreign cluster.

## Planned strategies

| Engine | Strategies | Failover |
|---|---|---|
| MariaDB | maxscale | MaxScale |

Their descriptor entries exist; their roles and hooks do not.

## Default ports

Every default differs from the product standard so the lab can sit next to an existing install.

| Engine | Component | Standard | Lab default |
|---|---|---|---|
| MySQL | server | 3306 | 13306 |
| MySQL | mysqld_exporter | 9104 | 19104 |
| MySQL | Orchestrator HTTP / raft | 3000 / 10008 | 13000 / 13008 |
| MySQL | Router RW / RO | 6446 / 6447 | 16446 / 16447 |
| MySQL | Galera gcomm / IST / SST | 4567 / 4568 / 4444 | 14567 / 14568 / 14444 |
| MariaDB | server | 3306 | 13306 (one engine per lab) |
| MariaDB | Galera gcomm / IST / SST | 4567 / 4568 / 4444 | 14567 / 14568 / 14444 |
| MariaDB | MaxScale RW / admin | 4006 / 8989 | 14006 / 18989 |
| PostgreSQL | server | 5432 | 15432 |
| PostgreSQL | postgres_exporter | 9187 | 19187 |
| PostgreSQL | Patroni REST API | 8008 | 18008 |
| PostgreSQL | etcd client / peer | 2379 / 2380 | 12379 / 12380 |
| core | node_exporter / lab collector / iperf | 9100 / - / 5201 | 19100 / 19106 / 15201 |

## Out of scope

- Mixing engines inside one cluster.
- Cross-engine replication.
- Claiming equivalence between engines: results are compared per architecture and measured, not assumed.
