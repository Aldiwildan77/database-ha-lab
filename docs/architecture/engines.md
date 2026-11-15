# Multiple database engines and HA strategies

Status: step 1 and part of step 2 are implemented (see "Implemented" below). Roles are not moved yet, and no second engine exists.

## Implemented

- `ansible/engines/<name>.yml` is the engine descriptor. `mysql.yml` holds, per HA strategy, the node limits, mesh and client ports, and the roles that provision and deploy it. It also holds packages, conflicting packages, process names, service name and the marker directory.
- `lab_config.py` loads every descriptor, reads `db_engine` from `ha.yml` (default `mysql`) and validates the engine and strategy pair. It exposes `db_engine`, `lab_engine` and `lab_ha_strategy`.
- `provision.yml`, `deploy-ha.yml`, `destroy-ha.yml` and `check-host.yml` read the descriptor instead of hard-coded MySQL names and `ha_provider` conditions.
- Behavior check: for all 5 MySQL strategies the mesh ports, client ports and minimum node counts match the previous hard-coded values.

- Strategy traits in the descriptor (`family`, `failover`, `semi_sync`) replace the `ha_provider` name checks in `ha-health.yml`, `failover.yml`, `recover.yml`, the replication role and the benchmark context. The runner reads `auto_failover` from `context.json`.

- Ports: each descriptor has a `ports` map (`default`, `standard`, `config` path in `ha.yml`, optional `span`) plus `primary_port` and `exporter_port`. The plugin keeps only the core ports (node_exporter, lab_collector, iperf). Port validation (range, distinct, span) runs over the descriptor's ports. `db_port` is the engine-neutral variable; `mysql_port` remains for the MySQL roles.

- Metrics: the collector is split into `ha_lab_collector.py` (generic loader) and `engine_mysql.py`. The neutral names are `ha_lab_db_up` and `ha_lab_replication_position` (were `ha_lab_mysql_up` and `ha_lab_gtid_executed_transactions`). The Prometheus job is `db` (was `mysqld`). The descriptor names the exporter role, exporter name, up metric and collector module. MySQL-only recording rules moved to `rules/engine-mysql.yml`, and the availability alert is `DatabaseDown` on `ha_lab_db_up`. Renamed metrics break old dashboards and saved queries; the generated dashboards are updated.

- JMeter: engine SQL differences live in `jmeter/scripts/dialects/<engine>.groovy` (class `LabDialect`: JDBC URL, read-only probe, `now()`, schema DDL, dataset upsert, error-code classification). `LabDb`, `prepare` and `transaction` call the dialect, selected by the `db_engine` JMeter property that the runner sets from `context.json`. The driver jar per engine is listed in `jmeter/drivers/<engine>.env`, and `make jmeter-install` installs the driver of the configured engine. The `mysql` dialect ran against a MySQL 8.4 container (12,176 samples, 0 errors). The `mariadb` and `postgres` dialects are untested.
- Faults: `stop-db` and `kill-db` replace `stop-mysql` and `kill-mysql` (both old names still work). `fault.yml` takes the service and process name from the descriptor.

Still hard-coded: the ledger and consistency SQL in `consistency.yml`, the replication-lag query in `report.py`, `mysql_*` variable names, the `lab-collector` role's credentials and socket path, the collectors, JMeter, and the roles' location under `ansible/roles/`.

## Planned engines

`ansible/engines/mariadb.yml` and `postgres.yml` have `status: planned`. They define versions, ports, packages, conflicting packages and HA strategies, but the roles they name (`mariadb`, `mariadb-replication`, `mariadb-galera`, `maxscale`, `postgres`, `postgres-replication`, `etcd`, `patroni`) do not exist. Selecting one fails at validate time with a clear message. The plugin checks every descriptor at load, so a typo in a port name or a missing key fails early.

| Engine | Strategies | Failover |
|---|---|---|
| MariaDB | async-replication, semi-sync | manual |
| MariaDB | galera | built in (Galera) |
| MariaDB | maxscale | MaxScale |
| PostgreSQL | streaming-replication, sync-replication | manual |
| PostgreSQL | patroni (etcd) | Patroni |

When an engine's roles land, its status becomes `ready`. The new `failover` values (`maxscale`, `patroni`) also need handling in `failover.yml` and the runner's `auto_failover` flag.

## Planned default ports

Every default differs from the product standard so the lab can sit next to an existing install. These are proposals for the engines that do not exist yet; each becomes the `ports` map of its descriptor.

| Engine | Component | Standard | Lab default |
|---|---|---|---|
| MySQL (implemented) | server | 3306 | 13306 |
| MariaDB | server | 3306 | 13306 (one engine per lab) |
| MariaDB | Galera gcomm / IST / SST | 4567 / 4568 / 4444 | 14567 / 14568 / 14444 |
| MariaDB | mysqld_exporter | 9104 | 19104 |
| PostgreSQL | server | 5432 | 15432 |
| PostgreSQL | postgres_exporter | 9187 | 19187 |
| PostgreSQL | Patroni REST API | 8008 | 18008 |
| PostgreSQL | etcd client / peer | 2379 / 2380 | 12379 / 12380 |

## Goal

Provision, monitor, benchmark and compare more than one database engine (MySQL, MariaDB, PostgreSQL) and more than one HA strategy per engine, without changing the core of the lab.

## Where MySQL is hard-coded today

| Area | Files | Assumption |
|---|---|---|
| Config parsing | `ansible/plugins/inventory/lab_config.py` | `mysql_*` variables, MySQL default ports, MySQL provider names |
| Roles | `ansible/roles/mysql`, `replication`, `orchestrator`, `innodb-cluster`, `percona-xtradb-cluster` | MySQL packages, `mysql` service, MySQL SQL |
| Host check | `ansible/playbooks/check-host.yml` | Conflicting packages and process names are MySQL-family |
| Metrics | `metrics/collectors/ha_lab_collector.py`, `mysql-exporter` | Performance Schema, `mysqld_exporter` |
| Load test | `jmeter/scripts/LabDb.groovy` | Connector/J, MySQL JDBC URL, MySQL error classes |
| Benchmark | `benchmark/runner/run.py`, `validation/consistency.py` | GTID-based RPO, MySQL fault commands |

## Target layout

```text
ansible/roles/
  core/                  common, firewall, node-exporter, lab-collector (engine-agnostic)
  engines/
    mysql/               install, configure, accounts, health, metrics queries
    mariadb/
    postgres/
  ha/
    mysql/{async-replication,semi-sync,orchestrator,innodb-cluster,percona-xtradb-cluster}/
    mariadb/{async-replication,galera}/
    postgres/{streaming-replication,patroni}/
```

## Contracts

### Engine plugin

Each `engines/<name>` provides, as a fixed set of task files and one `engine.yml` descriptor:

- Descriptor: `default_ports`, `packages`, `conflicting_packages`, `service_name`, `process_names`, `jdbc` (driver, URL template, error-class map), `supported_ha`.
- Tasks: `install`, `configure`, `accounts`, `health`, `collect` (emits the standard metrics), `wipe` (guarded by `DATA=1`).

### HA strategy plugin

Each `ha/<engine>/<strategy>` provides: `deploy`, `topology`, `switchover`, `failover`, `recover`, `destroy`, plus `min_nodes`.

### Metric contract

Every engine's collector emits the same `ha_lab_*` names and the same replication and transaction series that `metrics/schema/metrics.yml` defines. Dashboards, recording rules, the benchmark runner and the report read only those names. Engine-specific series (for example `mysql_perf_schema_*`) stay in their own namespace and are never required.

### Selection

`config/ha.yml` gains `db_engine` (default `mysql`, so current configs keep working) beside `ha_provider`. The inventory plugin validates the pair against `supported_ha` and fails early on an invalid combination, for example `orchestrator` with `postgres`.

## Engine notes

- **MariaDB**: close to MySQL but not identical. GTID format differs, authentication plugins differ, and its Galera is MariaDB Galera, not PXC. It needs its own engine role.
- **PostgreSQL**: a different model. Streaming replication, with Patroni or repmgr for failover. Topology, promotion and RPO measurement all need engine-specific code (LSN instead of GTID). Expect the most new code.

## Migration plan

1. **Refactor with no behavior change.** Move MySQL into `engines/mysql` and `ha/mysql/*`, add `db_engine: mysql` as the default, keep every Make target and config key working. Gate: the existing container tests (deploy, switchover, failover, recover, destroy, semi-sync, collector, JMeter run, report) pass unchanged.
2. **Genericize the core.** Host check, mesh check, firewall ports, collector framework and benchmark runner read the engine descriptor instead of MySQL constants.
3. **Add MariaDB.** Async replication first, then Galera.
4. **Add PostgreSQL.** Streaming replication first, then Patroni.

Each step lands separately and keeps master working.

## Out of scope

- Mixing engines inside one cluster.
- Cross-engine replication.
- Claiming equivalence between engines: results are compared per architecture and measured, not assumed.
