---
name: lab-node-check
description: How many VMs each database and test scenario needs (DB | scenario | HA strategy | VMs | arch), and whether a VM or server can be a database-ha-lab node. Use when the user offers a machine (IP, hostname, UTM/Proxmox/cloud VM) for the lab, asks if a machine is suitable, or asks how many servers a scenario needs.
---

# Lab node check

Two questions: **can this machine be a node**, and **are there enough nodes for the chosen scenario**. Probe read-only; never change a machine without the user's go-ahead. Machines may run other services: report conflicts, never stop or remove anything.

## 1. VMs needed per scenario

✅ ready · ❌ planned (refused by `make validate-config`). "VMs" = database nodes; JMeter, Prometheus and Grafana run on this machine. Min and max come from `ha.<strategy>` in `ansible/engines/<engine>/engine.yml`; re-read it if this looks stale.

| DB | Scenario | HA strategy | VMs needed | Arch | Notes |
|---|---|---|---|---|---|
| MySQL | Load benchmark (`baseline`, `read-heavy`, `write-heavy`, `mixed-workload`, `spike-test`, `soak-test`) | none, direct to one node | 1 ✅ | x86_64 | `ENDPOINT=<node>`; no HA in the path |
| MySQL | Load benchmark through the HA endpoint | any ready strategy | strategy minimum ✅ | x86_64 | `ENDPOINT=ha` |
| MySQL | Switchover (`make switchover`) | `async-replication`, `semi-sync` | 2 ✅, 3 recommended | x86_64 | No data loss expected |
| MySQL | Failover, manual promote (`failover-test`, `stop-db` / `kill-db`, `make promote`) | `async-replication`, `semi-sync` | 2 ✅, 3 recommended | x86_64 | With 2, no replica is left after failover; semi-sync then waits `timeout_ms` per commit |
| MySQL | Failover, automatic | `orchestrator` | 3 ✅ (odd) | x86_64 | Raft majority |
| MySQL | Failover, automatic | `innodb-cluster` | 3 ✅, max 9 (odd) | x86_64 | Group Replication quorum; Router on every node |
| MySQL | Failover, multi-primary | `percona-xtradb-cluster` | 3 ✅ (odd) | x86_64 | Galera quorum; failover/recover not yet verified |
| MySQL | Recover old primary (`make recover`) | any ready strategy | same as failover ✅ | x86_64 | Errant GTIDs stop it |
| MySQL | Network disruption (`network-disruption`, `isolate`, `netem`) | replication strategies | 2 ✅, 3 recommended | x86_64 | |
| MySQL | Quorum loss / split brain (`isolate` on a cluster node) | `orchestrator`, `innodb-cluster`, `percona-xtradb-cluster` | 3 ✅; 5 to survive 2 failures | x86_64 | Minority side must stop accepting writes |
| MariaDB | Load benchmark, direct | none | 1 ✅ | x86_64, aarch64 | |
| MariaDB | Switchover | `async-replication`, `semi-sync` | 2 ✅, 3 recommended | x86_64, aarch64 | |
| MariaDB | Failover, manual promote | `async-replication`, `semi-sync` | 2 ✅, 3 recommended | x86_64, aarch64 | semi-sync waits for exactly 1 replica |
| MariaDB | Recover old primary | `async-replication`, `semi-sync` | 2 ✅ | x86_64, aarch64 | Errant GTID domains stop it |
| MariaDB | Network disruption | `async-replication`, `semi-sync` | 2 ✅, 3 recommended | x86_64, aarch64 | |
| MariaDB | Failover, multi-primary | `galera` | 3 ❌ | – | planned |
| MariaDB | Failover, automatic via proxy | `maxscale` | 3 ❌ | – | planned |
| PostgreSQL | Load benchmark, direct | none | 1 ✅ | x86_64, aarch64 | |
| PostgreSQL | Switchover | `streaming-replication`, `sync-replication` | 2 ✅, 3 recommended | x86_64, aarch64 | Clean stop of the old primary, no data loss |
| PostgreSQL | Failover, manual promote | `streaming-replication` | 2 ✅, 3 recommended | x86_64, aarch64 | Picks the highest replay LSN |
| PostgreSQL | Failover, manual promote | `sync-replication` | 3 ✅ (2 works, but losing the standby blocks commits) | x86_64, aarch64 | Survives `nodes - 1 - num_sync` standby losses |
| PostgreSQL | Recover old primary (pg_rewind) | `streaming-replication`, `sync-replication` | 2 ✅ | x86_64, aarch64 | Needs `db_admin_password`; else `postgres_rebuild=true` |
| PostgreSQL | Network disruption | `streaming-replication`, `sync-replication` | 2 ✅, 3 recommended | x86_64, aarch64 | |
| PostgreSQL | Failover, automatic | `patroni` | 3 ✅ (odd; 5 survives 2 failures) | x86_64, aarch64 | etcd runs on the same nodes; leader key TTL 30 s bounds a hung leader |
| PostgreSQL | Switchover | `patroni` | 3 ✅ | x86_64, aarch64 | Patroni REST `/switchover`; with `synchronous_mode` only to the sync standby |
| PostgreSQL | Recover old leader | `patroni` | 3 ✅ | x86_64, aarch64 | Patroni rewinds it itself; `postgres_rebuild=true` runs `patronictl reinit` |
| PostgreSQL | Quorum loss (etcd) | `patroni` | 3 ✅ | x86_64, aarch64 | Losing 2 of 3 etcd members demotes the leader: read-only, no loss |

All nodes in a lab run one engine and one strategy. Switching needs `make destroy` (`DATA=1` when the server packages change).

## 2. Per-node requirements

| Check | Requirement | How it fails |
|---|---|---|
| OS | Ubuntu 22.04 / 24.04 or Debian 12 | `make validate` refuses others (TencentOS, Rocky, Proxmox host itself is Debian but don't use it) |
| Architecture | MySQL (all strategies, PXC): x86_64 only. MariaDB, PostgreSQL: x86_64 or aarch64 | descriptor `architectures`; validate refuses |
| CPU | 2+ cores | works with 1, latency numbers become noise |
| RAM | 2 GB+ free for the lab. Less: set `buffer_pool_ratio` (MySQL/MariaDB) or `shared_buffers_ratio` (PostgreSQL) to 0.25 | `make plan-host` refuses MySQL when the buffer pool + 512 MB exceeds MemAvailable |
| Disk | 20 GB+ free on `/` | dataset, binlogs/WAL (`wal_keep_size` 1 GB) |
| SSH | key login as `ssh.user` from this machine | validate |
| sudo | passwordless for `ssh.user`, or run every target with `ARGS=-K` | validate "Verify sudo works" |
| Network, node to node | every node reaches every other on the strategy's mesh ports | `make mesh-check` |
| Network, this machine to node | client port + monitoring ports reachable (Tailscale, VPN or same LAN). Docker Desktop containers are not reachable; UTM Shared Network VMs are | validate, `make metrics-check` |
| Internet from node | apt mirrors, GitHub releases, the engine repo (repo.mysql.com, deb.mariadb.org, apt.postgresql.org); mainland-China regions may be blocked | provision |
| Existing database | none of the engine's packages / datadir, unless adopting (`<engine>_adopt_existing`) | `make plan-host` |
| Time | chrony is installed by provisioning; another time daemon is replaced unless kept | provision |

Lab ports (defaults differ from product standards; all configurable):

| Strategy | Mesh ports (node to node) |
|---|---|
| MySQL / MariaDB replication | 13306 |
| PostgreSQL replication | 15432 |
| `patroni` | 15432, 18008 (Patroni REST), 12379, 12380 (etcd client, peer) |
| `orchestrator` | 13306, 13000, 13008 |
| `innodb-cluster` | 13306 (+ Router 16446/16447 for clients) |
| `percona-xtradb-cluster` | 13306, 14567, 14568, 14444 |
| Every node, monitoring | 19100 node_exporter, 19104 mysqld_exporter or 19187 postgres_exporter, 19106 collector |

## 3. Probe a machine

Find a login that works (try the user's usual names, `ubuntu`, then ask), then run read-only:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=8 <user>@<host> '
. /etc/os-release; echo "os: $PRETTY_NAME"; echo "arch: $(uname -m)"; echo "cpu: $(nproc)"
free -m | awk "/Mem/{print \"ram_mb: \" \$2 \" total, \" \$7 \" available\"}"
df -h / | awk "NR==2{print \"disk: \" \$4 \" free of \" \$2}"
sudo -n true 2>/dev/null && echo "sudo: passwordless" || echo "sudo: needs password"
ip -4 -br a | grep -v "^lo"
for s in mysql mariadb postgresql docker pveproxy; do systemctl is-active -q $s 2>/dev/null && echo "running: $s"; done
ss -ltnH | awk "{print \$4}" | grep -E ":(13306|15432|19100|19104|19106|19187|13000|13008|14444|14567|14568|16446)$" && echo "^ lab ports already in use"
'
```

From this machine also check reachability: `nc -z -G 3 <host> 22` (macOS) and, on Tailscale, `tailscale ping <host>`.

UTM VMs: `utmctl list`, `utmctl start "<vm>"`, `utmctl ip-address "<vm>"`. With the guest agent, `utmctl exec "<vm>" --cmd /bin/sh -c '<probe>'` runs as root without a login.

## 4. Report

One row per machine, then the verdict:

| Machine | OS | Arch | CPU | RAM free | Disk free | sudo | Reachable | Conflicts | Usable for |
|---|---|---|---|---|---|---|---|---|---|

- "Usable for": the engines its architecture allows, and a RAM note when under 2 GB free.
- Then: which scenarios the full set of machines can run (count of usable nodes per architecture vs section 1), and what is missing (for example "MySQL needs 2 more x86_64 nodes").
- Mixed architectures: MariaDB replication works across x86_64 and aarch64. PostgreSQL physical replication across architectures is not officially supported; prefer one architecture. MySQL needs x86_64 everywhere.
- Never suggest using a hypervisor host (Proxmox) or a machine running important services as a node without saying so; suggest VMs instead. Always run `make plan-host` before `make provision` on a shared machine.
