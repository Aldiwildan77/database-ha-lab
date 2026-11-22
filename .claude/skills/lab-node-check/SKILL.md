---
name: lab-node-check
description: Check whether a VM or server can be a database-ha-lab node, and how many nodes each engine and HA strategy needs. Use when the user offers a machine (IP, hostname, UTM/Proxmox/cloud VM) for the lab, asks if a machine is suitable, or asks how many servers a scenario needs.
---

# Lab node check

Two questions: **can this machine be a node**, and **are there enough nodes for the chosen scenario**. Probe read-only; never change a machine without the user's go-ahead. Machines may run other services: report conflicts, never stop or remove anything.

## 1. Nodes per HA scenario

Source of truth: `ha.<strategy>.min_nodes` / `max_nodes` / `status` in `ansible/engines/<engine>/engine.yml`. Re-read it if this table looks stale.

| Engine | Strategy | Status | Min | Recommended | Max | Why the recommendation |
|---|---|---|---|---|---|---|
| MySQL | `async-replication` | ready | 2 | 3 | – | With 2, a failover leaves no replica |
| MySQL | `semi-sync` | ready | 2 | 3 | – | With 2, losing the replica makes every commit wait `timeout_ms`, then fall back to async |
| MySQL | `orchestrator` | ready | 3 | 3 or 5 (odd) | – | Orchestrator raft needs a majority |
| MySQL | `innodb-cluster` | ready | 3 | 3 or 5 (odd) | 9 | Group Replication quorum; 9 is the GR limit |
| MySQL | `percona-xtradb-cluster` | ready | 3 | 3 or 5 (odd) | – | Galera quorum; an even split blocks writes |
| MariaDB | `async-replication` | ready | 2 | 3 | – | As MySQL async |
| MariaDB | `semi-sync` | ready | 2 | 3 | – | As MySQL semi-sync |
| MariaDB | `galera` | planned (refused) | 3 | – | – | – |
| MariaDB | `maxscale` | planned (refused) | 3 | – | – | – |
| PostgreSQL | `streaming-replication` | ready | 2 | 3 | – | With 2, a failover leaves no standby |
| PostgreSQL | `sync-replication` | ready | 2 | 3 | – | With 2 and `num_sync: 1`, losing the standby blocks all commits |
| PostgreSQL | `patroni` | planned (refused) | 3 | – | – | – |

One node can run provisioning, monitoring and direct benchmarks; `make ha` refuses below `min_nodes`. All nodes of a lab run the same engine and strategy.

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
