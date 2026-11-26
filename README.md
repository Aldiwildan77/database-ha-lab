# database-ha-lab

Deploy, monitor, benchmark and compare database high-availability setups (MySQL, MariaDB, PostgreSQL) across servers and data centers. Remote nodes run the database and exporters, provisioned by Ansible. Your machine runs Ansible, Prometheus, Grafana and JMeter. Nodes talk over Tailscale, a private LAN or your own VPN.

```text
 LOCAL MACHINE                                      REMOTE NODES (x N, any DC)
 ┌──────────────────────────────┐                  ┌──────────────────────────────┐
 │ Ansible ── provision ────────┼── SSH ──────────▶│ MySQL | MariaDB | PostgreSQL │
 │ Prometheus ── scrape ────────┼── :19100/:19104 ▶│ node + engine exporter       │
 │ Grafana (dashboards)         │    /:19187/:19106│ lab collector, network probe │
 │ JMeter ──────────────────────┼── :13306/:15432 ▶│ chrony, nftables             │
 └──────────────────────────────┘                  └──────────────────────────────┘
```

## HA strategies

Set `db_engine` and `ha_provider` in `config/ha.yml`.

| Engine | Strategy | Failover | Min nodes |
|---|---|---|---|
| MySQL 8.4 / 8.0 | `async-replication`, `semi-sync` | manual | 2 |
| | `orchestrator` | Orchestrator (raft) | 3 |
| | `innodb-cluster` | Group Replication + Router | 3 |
| | `percona-xtradb-cluster` | Galera | 3 |
| MariaDB 11.4 / 10.11 / 11.8 | `async-replication`, `semi-sync` | manual | 2 |
| | `galera` | Galera (multi-primary) | 3 |
| PostgreSQL 17 / 16 / 18 | `streaming-replication`, `sync-replication` | manual, `pg_rewind` recovery | 2 |
| | `patroni` | Patroni (etcd) | 3 |

- ✅ MySQL `async-replication`: tested end to end on real Ubuntu hosts over WireGuard
- ☑️ All others: tested end to end in containers; real hosts pending

> [!WARNING]
> **Switching strategy or engine:** run these in order (`make destroy` reads the *current* `config/ha.yml`).
> 1. `make destroy`, or `make destroy DATA=1` when changing engine (**deletes all data**)
> 2. Edit `ha_provider` / `db_engine` in `config/ha.yml`
> 3. `make provision` if the engine changed
> 4. `make ha`

## Requirements

**Your machine**
- [ ] Ansible core 2.17+, Docker with Compose v2, `python3`, `curl`, `openssl`, `nc`
- [ ] Java 8+ for JMeter (`make jmeter-install`)
- [ ] Chrome or Chromium for `make screenshots`, `make pdf` and `make manual`
- [ ] Tailscale, only if a node uses a `tailscale` network

**Each node**
- [ ] An apt-based OS with `python3` (tested: Ubuntu 22.04, 24.04, 26.04, Debian 12)
- [ ] x86_64 for MySQL (arm64 works for MariaDB and PostgreSQL)
- [ ] SSH with a sudo user, set per node under `ssh:` in `config/inventory.yml`
- [ ] Reachable from your machine and from every other node
- [ ] Outbound access to the engine's package repo and GitHub releases

## Quick start

```bash
make deps && make init                          # collections, config/*.yml from examples
$EDITOR config/inventory.yml config/ha.yml config/secrets.yml
ansible-vault encrypt config/secrets.yml
export ANSIBLE_VAULT_PASSWORD_FILE=~/.lab-vault-pass

make validate        # tools, config, SSH, OS, full-mesh connectivity
make plan-host       # read-only: what provision would change, conflicts
make provision       # database, exporters, collector, firewall
make monitoring      # Prometheus + Grafana on 127.0.0.1
make ha && make topology

make jmeter-install
make benchmark SCENARIO=mixed-workload
make failover CONFIRM=yes [FAULT=stop-db|kill-db|isolate|freeze|partition-client]
make screenshots && make pdf                    # report into reports/<db>/<ha>/<scenario>/
```

Grafana: http://127.0.0.1:3000, user `admin`, password in `local/compose/.env`.

## Commands

```text
Configuration   make init | deps | validate | validate-local | validate-config
Infrastructure  make plan-host | provision | configure-exporters | collect-info
Monitoring      make targets | monitoring | monitoring-stop | monitoring-status | metrics-check
Network         make mesh-check | network-check | network-matrix [IPERF=1]
HA              make ha | topology | switchover [NEW_PRIMARY=] | promote [NEW_PRIMARY=] [FORCE=1] | recover NODE=
Faults          make fault NODE= FAULT=stop-db|kill-db|isolate|netem|degrade|freeze|slow-disk|partition-client [ACTION=restore] CONFIRM=yes
Benchmark       make benchmark SCENARIO= [ENDPOINT=ha|primary|<node>] [J="k=v ..."] | failover CONFIRM=yes [FAULT=]
Chaos           make chaos SCENARIO=<s> PLAN="180:kill-db 360:freeze/60@<node>" CONFIRM=yes
Local VMs       make local-lab [ACTION=up|down|status] [N=3]   then export LAB_CONFIG_DIR=local/lab-vms/config
JMeter GUI      make jmeter-ui SCENARIO=<s>   (same properties as a run; nothing recorded)
Suites          make suite SUITE=benchmark/suites/<f>.yml [REPEAT=n] [REPORTS=1] [CONFIRM=yes] | compare RUNS="run-a run-b"
Reports         make report | screenshots | pdf [RUN=<run_id>] | manual
Teardown        make destroy [DATA=1]
Development     make lint | scenarios | dashboards
```

- Scenarios: `baseline`, `read-heavy`, `write-heavy`, `mixed-workload`, `spike-test`, `soak-test`, `failover-test`, `network-disruption`, `wave-test`, `rate-test`
- `rate-test`: fixed request rate that does not drop when latency rises, e.g. `J="rate=50 burst_threads=200 burst_rate=170"` sends 10k requests in the 60 s burst. Keep threads >= rate x latency (s) and under `max_connections`.
- `wave-test`: 10 min, load rises and falls (4, 16, 48 threads, a 112-thread burst at 5:00-5:30, then back down)
- Gray failures: `freeze` (database hung by SIGSTOP), `slow-disk` (`-e disk_rate=1M -e disk_iops=50`), `partition-client` (clients lose the node, nodes still see each other). Every fault starts on the node 3 s after injection returns and records whether it was really active.
- `degrade` fault: the node stays up but barely works (stress-ng on CPU, disk and memory, plus netem, default `delay 300ms 30ms loss 10%`). Example:
  `make benchmark SCENARIO=wave-test FAULT=degrade CONFIRM=yes RUN_ARGS="--fault-node db-02 --fault-at 120 --fault-duration 240"`
- Extra Ansible args: `make provision ARGS="--limit db-02 --diff"`
- Published reports: [reports/](reports/), named `<db>-<ver>_<ha>_<scenario>[_<fault>]_<date>-<seq>.pdf`, IPs redacted

## Ports

Lab ports never use the product defaults, so the lab can share a host with existing services.

| Component | Lab port |
|---|---|
| MySQL / MariaDB, PostgreSQL | 13306, 15432 |
| node_exporter, mysqld / postgres exporter, lab collector | 19100, 19104 / 19187, 19106 |
| Orchestrator, MySQL Router RW / RO | 13000, 16446 / 16447 |
| Galera gcomm / IST / SST, Patroni / etcd | 14567 / 14568 / 14444, 18008 / 12379 |

## Safety

- 🛡️ `make plan-host` is read-only; `provision` stops on a used port, a conflicting DB package, or a datadir the lab did not create
- 🛡️ No split brain: a restarted old primary always comes back read-only and must be rejoined with `make recover`
- 🛡️ Replicas with errant GTIDs or divergent data are never attached
- 🛡️ Faults need `CONFIRM=yes`; `make destroy` asks for the environment name
- 🛡️ Firewall: its own nftables table `inet halab` for lab ports only (plus narrow ufw rules when ufw is active)
- 🛡️ Real config, secrets and results are gitignored; only `*.example.yml` and `reports/` are committed

## Docs

- [Operator manual (PDF)](docs/manuals/database-ha-lab-manual.pdf): sizing, networks, every strategy and scenario, troubleshooting
- [HA runbooks](ha/README.md) · [Benchmarking](docs/runbooks/benchmarking.md) · [Engines](docs/architecture/engines.md) · [Networks](docs/architecture/networks.md) · [Diagrams](docs/architecture/diagrams.md) · [Design](docs/architecture/foundation.md)
