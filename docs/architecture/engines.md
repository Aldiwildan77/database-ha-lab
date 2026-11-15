# Multiple database engines and HA strategies

Status: design proposal. Nothing here is implemented yet.

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
