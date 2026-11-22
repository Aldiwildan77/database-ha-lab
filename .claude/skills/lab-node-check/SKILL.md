---
name: lab-node-check
description: Print the lab sizing tables - for every database type and test scenario, which HA strategy, how many VMs / bare-metal servers, the minimum and recommended spec per node, and the CPU architecture. Use when the user asks how many servers or what spec a scenario needs.
---

# Lab sizing tables

Print the tables below as they are. Do not probe or log in to any machine. If the user names a scenario or engine, print only the matching rows plus the table headers.

Status: ✅ ready · ❌ planned (refused by `make validate-config`). Counts and status come from `ha.<strategy>` (`min_nodes`, `max_nodes`, `status`) in `ansible/engines/<engine>/engine.yml`; if those changed, update this file first.

## Scenarios

"VMs" are database nodes, virtual or bare metal. JMeter, Prometheus and Grafana run on the operator's machine. Spec is per node: vCPU / RAM / disk. "Min" is enough for correctness tests (switchover, failover, recover, RPO) with the default 100k-row dataset; "Recommended" is for benchmarks you will compare.

| DB | Scenario | HA strategy | VMs needed | Min spec | Recommended spec | Arch | Notes |
|---|---|---|---|---|---|---|---|
| MySQL | Load benchmark, direct (`baseline`, `read-heavy`, `write-heavy`, `mixed-workload`, `spike-test`, `soak-test`) | none | 1 ✅ | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64 | `ENDPOINT=<node>`; `soak-test` +20 GB disk |
| MySQL | Load benchmark through the HA endpoint | any ready strategy | strategy minimum ✅ | as the strategy | as the strategy | x86_64 | `ENDPOINT=ha` |
| MySQL | Switchover | `async-replication`, `semi-sync` | 2 ✅, 3 recommended | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64 | No data loss expected |
| MySQL | Failover, manual promote (`failover-test`, `stop-db`, `kill-db`) | `async-replication`, `semi-sync` | 2 ✅, 3 recommended | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64 | With 2, no replica is left after failover |
| MySQL | Failover, automatic | `orchestrator` | 3 ✅ (odd) | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64 | Raft majority |
| MySQL | Failover, automatic | `innodb-cluster` | 3 ✅, max 9 (odd) | 2 / 4 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64 | Group Replication + Router on every node |
| MySQL | Failover, multi-primary | `percona-xtradb-cluster` | 3 ✅ (odd) | 2 / 4 GB / 30 GB | 4 / 8 GB / 50 GB SSD | x86_64 | SST needs room for a second data copy |
| MySQL | Recover old primary | any ready strategy | same as failover ✅ | as the strategy | as the strategy | x86_64 | Errant GTIDs stop it |
| MySQL | Network disruption (`network-disruption`, `isolate`, `netem`) | any ready strategy | 2 ✅ (clusters 3), 3 recommended | as the strategy | as the strategy | x86_64 | |
| MySQL | Quorum loss / split brain | `orchestrator`, `innodb-cluster`, `percona-xtradb-cluster` | 3 ✅; 5 to survive 2 failures | as the strategy | as the strategy | x86_64 | Minority must stop accepting writes |
| MariaDB | Load benchmark, direct | none | 1 ✅ | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64, aarch64 | |
| MariaDB | Switchover | `async-replication`, `semi-sync` | 2 ✅, 3 recommended | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64, aarch64 | |
| MariaDB | Failover, manual promote | `async-replication`, `semi-sync` | 2 ✅, 3 recommended | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64, aarch64 | semi-sync waits for exactly 1 replica |
| MariaDB | Recover old primary | `async-replication`, `semi-sync` | 2 ✅ | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64, aarch64 | Errant GTID domains stop it |
| MariaDB | Network disruption | `async-replication`, `semi-sync` | 2 ✅, 3 recommended | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64, aarch64 | |
| MariaDB | Failover, multi-primary | `galera` | 3 ❌ (odd) | 2 / 4 GB / 30 GB | 4 / 8 GB / 50 GB SSD | x86_64, aarch64 | planned |
| MariaDB | Failover, automatic via proxy | `maxscale` | 3 ❌ | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64, aarch64 | planned |
| PostgreSQL | Load benchmark, direct | none | 1 ✅ | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64, aarch64 | |
| PostgreSQL | Switchover | `streaming-replication`, `sync-replication` | 2 ✅, 3 recommended | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64, aarch64 | No data loss expected |
| PostgreSQL | Failover, manual promote | `streaming-replication` | 2 ✅, 3 recommended | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64, aarch64 | Picks the highest replay LSN |
| PostgreSQL | Failover, manual promote | `sync-replication` | 3 ✅ (2 works; losing the standby blocks commits) | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64, aarch64 | |
| PostgreSQL | Recover old primary (pg_rewind) | `streaming-replication`, `sync-replication` | 2 ✅ | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64, aarch64 | Needs `db_admin_password` |
| PostgreSQL | Network disruption | `streaming-replication`, `sync-replication` | 2 ✅, 3 recommended | 2 / 2 GB / 20 GB | 4 / 8 GB / 50 GB SSD | x86_64, aarch64 | |
| PostgreSQL | Failover, automatic | `patroni` | 3 ❌ (odd, etcd on the same nodes) | 2 / 4 GB / 20 GB SSD | 4 / 8 GB / 50 GB SSD | x86_64, aarch64 | planned; etcd needs fast fsync |

Lab total = VMs x spec. Example: PostgreSQL `streaming-replication`, recommended 3 nodes at minimum spec = 6 vCPU, 6 GB RAM, 60 GB disk.

## Every node

| Item | Minimum | Recommended | Notes |
|---|---|---|---|
| OS | Ubuntu 22.04 / 24.04, Debian 12 | same on all nodes | Others are refused by `make validate` |
| Disk type | any | SSD / NVMe | Shared or HDD disks make commit latency noisy |
| Network between nodes | 1 Gbit, every node reaches every other on the strategy's ports | < 1 ms in one DC | Multi-DC: semi-sync and sync replication pay one RTT per commit |
| Network from the operator's machine | SSH + client + monitoring ports reachable | Tailscale, VPN or same LAN | |
| VM CPU | any | `host` CPU type, no overcommit | |
| Access | SSH key login, passwordless sudo (or `ARGS=-K`) | | |
| Low RAM (2 GB) | set `buffer_pool_ratio` / `shared_buffers_ratio` to 0.25 | | |
| Operator's machine | 2 cores / 4 GB free | 4 cores / 8 GB free | JMeter + Docker Prometheus/Grafana |
