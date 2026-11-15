# Foundation: design decisions, assumptions, limitations

## Decisions

| Area | Decision | Reason |
|---|---|---|
| Inventory | Custom `lab_config` inventory plugin reads `config/*.yml` | One parser and validator for every Ansible command; user config stays simpler than Ansible inventory syntax |
| MySQL packages | Oracle APT repo (`mysql-8.4-lts` / `mysql-8.0`), pinned with priority 1001 | 8.4 LTS is not in the Ubuntu/Debian archives; MySQL Shell and Router (InnoDB Cluster) come from the same repo |
| Root access | `root@localhost` keeps `auth_socket` | No root password to manage or leak |
| Node-local accounts | Created with `sql_log_bin=0` | Prevents errant GTIDs once replication is configured |
| Server defaults | GTID on, `sync_binlog=1`, `innodb_flush_log_at_trx_commit=1`, Performance Schema transaction consumers on | Durable baseline shared by every HA provider so comparisons start from the same settings |
| Exporters | Official release binaries, SHA-256 verified, systemd with hardening | No third-party packages; versions pinned in `config/monitoring.yml` |
| Scrape address | Tailscale IP resolved on the controller at target generation (default) | Docker Desktop containers may not resolve MagicDNS |
| Prometheus labels | `environment, ha_provider, node, role, datacenter, fqdn` | Bounded cardinality; no per-transaction labels |
| Statement digests | `perf_schema.eventsstatements` collector disabled | `digest_text` label is unbounded |

## Assumptions

- Nodes and this machine are on one tailnet; MagicDNS is enabled; Tailscale ACLs permit SSH, 3306, 9100, 9104 from this machine and between nodes.
- Nodes have outbound internet access for package and binary downloads.
- `role` label is the *configured initial* role. After a failover the real primary differs; dashboards for HA (Phase 4+) read the actual role from MySQL metrics.

## Known limitations

- MySQL from repo.mysql.com is amd64-only; arm64 nodes are rejected.
- Percona XtraDB Cluster uses different server packages; provisioning refuses `ha_provider: percona-xtradb-cluster` until Phase 7.
- `my.cnf` changes restart mysqld through a handler. On a running cluster this causes a failover; use `ARGS="--limit <node>"` to roll changes one node at a time.
- `innodb_buffer_pool_size` defaults to 50% of RAM; override for small nodes.
- Disk latency on the Host dashboard is a derived mean from `/proc/diskstats`, not a distribution. Histograms arrive with Phase 5 collectors.
- Clock offset (`node_timex_offset_seconds`, chrony) is recorded but cross-node timestamp subtraction remains uncertain by at least that offset.
- ufw rules assume the iptables/nftables backend that ufw manages; review interactions if you run other firewall tooling.

## Supported versions

| Component | Version |
|---|---|
| OS | Ubuntu 22.04, 24.04; Debian 12 (x86_64) |
| MySQL | 8.4 LTS (default), 8.0 |
| node_exporter | 1.12.1 |
| mysqld_exporter | 0.21.0 |
| Prometheus | v3.15.1 |
| Grafana | 12.4.12 |
| Ansible collections | community.general 14.0.1, ansible.mysql 5.2.1, ansible.posix 2.2.2 |
