# Database engines and HA strategies

The lab core is engine-neutral. Everything specific to one database lives in that engine's folder, in its JMeter dialect and in its metrics collector. MySQL is ready. MariaDB is ready for `async-replication` and `semi-sync`; its `galera` and `maxscale` strategies and PostgreSQL are planned.

## Layout

```text
ansible/
  engines/
    mysql/                 ready
      engine.yml           descriptor: ports, packages, exporter, collector, HA strategies
      roles/               mysql, mysql-exporter, replication, orchestrator, innodb-cluster,
                           percona-xtradb-cluster, percona-repo
      tasks/               hooks called by the core playbooks (see "Engine hooks")
    mariadb/               ready (async-replication, semi-sync)
      engine.yml           descriptor; galera and maxscale are status: planned
      roles/               mariadb, mariadb-replication (reuses mysql-exporter)
      tasks/               hooks
    postgres/engine.yml    planned
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
| `config_section`, `versions`, `default_version` | Engine settings section in `ha.yml` (`mysql:`, `mariadb:`); the same key under a node holds per-node server options |
| `service`, `process_names`, `helper_processes`, `marker_dir`, `config_file` | Used by host checks, faults and markers; `config_file` is the lab settings file shown by `make plan-host` |
| `packages`, `conflicting_packages`, `apt_repository` | Per strategy or `default`; used by `make plan-host` |
| `ports`, `primary_port`, `exporter_port` | Lab defaults that differ from the product standard; `config` names the `ha.yml` override; `span` reserves consecutive ports |
| `exporter` | Role that installs it, its name, and its "up" metric |
| `collector` | Collector module, the system user it runs as, the unit it starts after, and how it connects |
| `ansible_collections` | Collections this machine needs for the engine (checked by `make validate-local`) |
| `report_queries` | Optional engine-only PromQL added to the report |
| `ha.<strategy>` | `family`, `failover`, traits (`semi_sync`, `synchronous`), node limits, mesh and client ports, `ha_endpoint`, and the roles for `provision` and `deploy` |

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
- Variables: `db_engine`, `db_port`, `db_exporter_port`, `lab_engine`, `lab_ha_strategy`, `lab_ha_endpoint`, `lab_engine_tasks`. MySQL roles keep their own `mysql_*` variables.
- Secrets: `db_exporter_password`, `db_replication_password`, `db_benchmark_password`, `db_admin_password` (env `LAB_DB_*_PASSWORD`). The older `mysql_*` keys and `LAB_MYSQL_*` env vars are still read.
- Metrics: `ha_lab_db_up`, `ha_lab_writable`, `ha_lab_read_only`, `ha_lab_replication_position`; the Prometheus job is `db`; the availability alert is `DatabaseDown`.
- Faults: `stop-db`, `kill-db` (`stop-mysql` and `kill-mysql` are aliases).
- The benchmark context records `db_engine` and `db_version`; the report's Database column shows both.

## Adding an engine

1. Write `ansible/engines/<engine>/engine.yml` (copy the MySQL one) with `status: planned`.
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

## Planned strategies and engines

| Engine | Strategies | Failover |
|---|---|---|
| MariaDB | galera | built in (Galera) |
| MariaDB | maxscale | MaxScale |
| PostgreSQL | streaming-replication, sync-replication | manual |
| PostgreSQL | patroni (etcd) | Patroni |

Their descriptors exist and, for PostgreSQL, so do the JMeter dialect and driver file. The roles, hooks and collectors do not, and the PostgreSQL dialect and driver version are untested.

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
