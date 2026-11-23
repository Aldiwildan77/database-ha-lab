# Foundation: design decisions, assumptions, limitations

## Decisions

| Area | Decision | Reason |
|---|---|---|
| Inventory | Custom `lab_config` inventory plugin reads `config/*.yml` | One parser and validator for every Ansible command; user config stays simpler than Ansible inventory syntax |
| Engines | One descriptor per engine in `ansible/engines/<engine>/engine.yml`, engine work in hooks | The core playbooks stay engine-neutral; see [engines.md](engines.md) |
| MySQL packages | Oracle APT repo (`mysql-8.4-lts` / `mysql-8.0`), pinned with priority 1001 | 8.4 LTS is not in the Ubuntu/Debian archives; MySQL Shell and Router (InnoDB Cluster) come from the same repo |
| MariaDB packages | deb.mariadb.org, pinned to `mariadb-*` packages only | Distribution archives lag the LTS series |
| PostgreSQL packages | apt.postgresql.org (PGDG); one cluster named `halab` per node | Every supported major version on every OS; other clusters on the host are never touched |
| Root access | MySQL: `root@localhost` keeps `auth_socket`; MariaDB: `unix_socket`. PostgreSQL: peer auth as `postgres` | No database root password to manage or leak |
| Node-local accounts | MySQL/MariaDB: created with `sql_log_bin=0`. PostgreSQL: created on the primary only | Prevents errant GTIDs; standbys get roles through replication |
| Server defaults | MySQL/MariaDB: GTID on, `sync_binlog=1`, `innodb_flush_log_at_trx_commit=1`. PostgreSQL: `synchronous_commit=on`, `wal_log_hints=on` | Durable baseline shared by every strategy of an engine so comparisons start from the same settings |
| Exporters | Official release binaries, SHA-256 verified, systemd with hardening | No third-party packages; versions pinned in `config/monitoring.yml` |
| Scrape address | Node address resolved on the controller at target generation (default) | Docker Desktop containers may not resolve MagicDNS or internal DNS |
| Prometheus labels | `environment, ha_provider, node, role, datacenter, fqdn` | Bounded cardinality; no per-transaction labels |
| Statement digests | `perf_schema.eventsstatements` collector disabled | `digest_text` label is unbounded |

## Assumptions

- Every node reaches every other node, and this machine reaches every node, on the networks defined in `config/inventory.yml` (see [networks.md](networks.md)). `make mesh-check` verifies this.
- Nodes have outbound internet access for package and binary downloads.
- `role` label is the *configured initial* role. After a failover the real primary differs; dashboards for HA (Phase 4+) read the actual role from the engine's metrics (`ha_lab_writable`).

## Known limitations

- MySQL from repo.mysql.com is amd64-only; arm64 nodes are rejected.
- Percona XtraDB Cluster uses different server packages. Choose `ha_provider` before provisioning; switching to or from PXC needs `make destroy DATA=1`.
- Server config changes (`my.cnf`, `postgresql.conf`) restart the server through a handler. On a running cluster this causes a failover; use `ARGS="--limit <node>"` to roll changes one node at a time.
- `innodb_buffer_pool_size` defaults to 50% of RAM (MySQL/MariaDB) and `shared_buffers` to 25% (PostgreSQL); override for small or shared nodes.
- Disk latency on the Host dashboard is a derived mean from `/proc/diskstats`, not a distribution. Histograms arrive with Phase 5 collectors.
- Clock offset (`node_timex_offset_seconds`, chrony) is recorded but cross-node timestamp subtraction remains uncertain by at least that offset.
- The default nftables table only adds drops for lab ports. Your other tables still apply, so lab ports must not be blocked by them. `make mesh-check` detects that.

## Ports

Every lab port defaults to a value that differs from the product's standard port. The lab can then run next to an existing database server, exporter, Orchestrator, Router or Galera on the same host without a port collision. All of them are configurable, and the inventory plugin rejects duplicates.

| Purpose | Lab default | Product standard | Setting |
|---|---|---|---|
| MySQL / MariaDB | 13306 | 3306 | `ha.yml` `mysql.port` / `mariadb.port` |
| PostgreSQL | 15432 | 5432 | `ha.yml` `postgres.port` |
| MySQL X Protocol | off | 33060 | not used by the lab |
| node_exporter | 19100 | 9100 | `ha.yml` `monitoring.node_exporter_port` |
| mysqld_exporter (MySQL, MariaDB) | 19104 | 9104 | `ha.yml` `monitoring.mysql_exporter_port` |
| postgres_exporter | 19187 | 9187 | `ha.yml` `monitoring.postgres_exporter_port` |
| Lab collector | 19106 | — | `ha.yml` `monitoring.lab_collector_port` |
| Orchestrator HTTP / raft | 13000 / 13008 | 3000 / 10008 | `ha.yml` `orchestrator.http_port`, `raft_port` |
| MySQL Router RW / RO | 16446 / 16447 | 6446 / 6447 | `ha.yml` `innodb_cluster.router.rw_port` |
| Galera group / IST / SST | 14567 / 14568 / 14444 | 4567 / 4568 / 4444 | `ha.yml` `percona_xtradb_cluster.gcomm_port`, `ist_port`, `sst_port` |
| iperf3 (network matrix) | 15201 | 5201 | `monitoring.yml` `iperf_port` |

Different ports do not let two *system* MySQL or MariaDB installations share a host. They would still share packages, `/var/lib/mysql` and the service, and `make plan-host` refuses that case. PostgreSQL is the exception: the lab's `halab` cluster runs beside existing clusters of the same or another major version.

## Supported versions

| Component | Version |
|---|---|
| OS | apt-based; tested Ubuntu 22.04, 24.04, Debian 12; others warn (MySQL x86_64 only; MariaDB and PostgreSQL also arm64) |
| MySQL | 8.4 LTS (default), 8.0 |
| MariaDB | 11.4 LTS (default), 10.11, 11.8 |
| PostgreSQL | 17 (default), 16, 18 |
| node_exporter | 1.12.1 |
| mysqld_exporter | 0.21.0 |
| postgres_exporter | 0.20.1 |
| Prometheus | v3.15.1 |
| Grafana | 12.4.12 |
| Ansible collections | community.general 14.0.1, ansible.mysql 5.2.1, community.postgresql 5.1.2, ansible.posix 2.2.2 |
| JMeter | 5.6.3 (Java 8+) |
| JDBC drivers | MySQL Connector/J 26.10.0, MariaDB Connector/J 3.5.3, PgJDBC 42.7.4 (`jmeter/drivers/`) |
| Orchestrator | percona-orchestrator 3.2.6 (Percona pdps-84-lts / pdps-80) |
| Percona XtraDB Cluster | 8.4 LTS / 8.0 |
| MySQL accounts | `caching_sha2_password` (8.4 does not load `mysql_native_password`) |
| PostgreSQL accounts | `scram-sha-256` |
