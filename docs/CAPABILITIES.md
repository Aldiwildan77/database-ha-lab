# Capabilities

Two separate ratings:
- **Feature**: what the code can do.
- **Tested**: what has run against real databases. Real servers: lab-hanif, lab-ivan (2 nodes, MySQL 8.4, PostgreSQL 17). Local VMs: 3 Lima VMs with PostgreSQL 17 + Patroni (`make local-lab`).

✅ yes · ⚠️ partial · ❌ no · ⏳ smoke run in progress

## Catalog

| Area | Capability | Feature | Tested |
|---|---|---|---|
| Engines | MySQL 8.4: async, semi-sync | ✅ | ✅ deploy, failover, rebuild, chaos (real servers) |
| | MySQL: Orchestrator, InnoDB Cluster, Percona XtraDB Cluster | ✅ | ❌ needs 3 x86_64 nodes |
| | MariaDB: async, semi-sync, Galera (MaxScale planned) | ✅ | ❌ |
| | PostgreSQL 16-18: streaming, sync | ✅ | ⚠️ deploy + degrade runs; no failover yet |
| | PostgreSQL: Patroni + etcd | ✅ | ✅ deploy on 3 VMs; ⏳ failover |
| Infrastructure | Provision with Ansible, plan-host dry run, validate, destroy (data purge optional) | ✅ | ✅ |
| | Networks per node: Tailscale, VPN, private LAN, mixed; separate operator network | ✅ | ✅ Tailscale + WireGuard, two private networks |
| | Mesh check, network matrix (RTT, loss, iperf) | ✅ | ✅ |
| | Config dir switch (`LAB_CONFIG_DIR`) | ✅ | ✅ |
| | Local VM lab (`make local-lab`, Lima, aarch64: MariaDB, PostgreSQL) | ✅ | ✅ 3 VMs provisioned |
| Operations | Switchover, promote, recover (CLONE / pg_rewind), topology | ✅ | ⚠️ MySQL only |
| | Safety: confirmations, "replica holds data" guard, no second primary on restore, fencing of a frozen primary, host-wide faults need `HOST_WIDE=1` | ✅ | ⚠️ guards seen firing; fencing untested |
| Faults | stop-db, kill-db, isolate (node-to-node), netem | ✅ | ✅ under load |
| | degrade (CPU, disk, memory + netem) | ✅ | ✅ under load |
| | freeze (SIGSTOP), slow-disk (cgroup IO cap), partition-client | ✅ | ✅ inject/restore on a real node; ⏳ under load |
| | clock-skew, disk-full | ✅ | ⏳ |
| | Pre-armed start (never over the broken link), auto-restore timers, active-at-restore check | ✅ | ✅ |
| | Chaos plan: several faults on a schedule in one run (`make chaos`) | ✅ | ⏳ |
| Load | 11 scenarios: steady, read/write heavy, spike, soak, wave, fixed rate, failover, network | ✅ | ✅ (soak not run) |
| | Workloads: `oltp`, `orders` (TPC-C-like, contended stock) | ✅ | ✅ orders: 448 TPS, 0 errors |
| | Reporting traffic next to OLTP (`report_threads`), summarized apart | ✅ | ✅ |
| | Fixed request rate (load does not drop when latency rises) | ✅ | ⚠️ verified with JMeter locally; ⏳ against a DB |
| | Remote load generator (`load_host`) | ✅ | ⏳ |
| | JMeter GUI with lab properties (`make jmeter-ui`) | ✅ | ❌ needs a display |
| | Suites with repeats (`make suite`) | ✅ | ⏳ |
| Correctness | Per-write ledger: lost, phantom, duplicate, unknown-outcome writes on every node | ✅ | ✅ caught real loss (run 022) |
| | Stale-read check: time until a commit is readable on a replica | ✅ | ✅ 11,483 writes, p99 7 ms, none missed |
| | Account balance checksum across nodes | ✅ | ✅ |
| Detection | Primary down = not answering at protocol level for 3 polls, re-checked before promotion | ✅ | ⚠️ fake-server tests; ⏳ live |
| Metrics | Prometheus + Grafana, node/engine exporters, lab collector, client probe, live JMeter metrics | ✅ | ✅ |
| Reports | report.md/html, timeline chart, Grafana screenshots (all or filtered), PDF with IP redaction | ✅ | ✅ |
| | Comparison: median, min-max, worst-case loss, grouped by HA settings; HTML charts + timelines | ✅ | ✅ on past runs |
| Quality | Unit tests (`make test`), lint, CI on every push (tests, lint, playbook syntax, JMeter plans, Groovy compile) | ✅ | ✅ CI green |

## Compared with other platforms (feature level)

Other columns come from public documentation and general knowledge, not from runs in this lab.

| Feature | Ours | Jepsen | sysbench / HammerDB | Chaos Mesh / Litmus | ClusterControl | autobase / Pigsty |
|---|---|---|---|---|---|---|
| Deploy HA on VMs / bare metal | ✅ 3 engines, 11 strategies | ⚠️ test setups | ❌ | ❌ | ✅ | ✅ PG only |
| One config for engine + strategy | ✅ | ❌ code per DB | ❌ | ❌ | ✅ GUI | ⚠️ |
| Crash / stop faults | ✅ | ✅ | ❌ | ✅ | ⚠️ manual | ❌ |
| Network partitions | ✅ node-to-node, client-side | ✅ richest | ❌ | ✅ | ❌ | ❌ |
| Gray failures (hung, slow disk, degraded) | ✅ | ⚠️ pause | ❌ | ✅ | ❌ | ❌ |
| Clock skew, disk full | ✅ | ✅ clock | ❌ | ✅ | ❌ | ❌ |
| Fault schedules | ✅ | ✅ generators | ❌ | ✅ workflows | ❌ | ❌ |
| Load: steady, spike, wave, fixed rate | ✅ | ⚠️ light | ✅ | ❌ | ❌ | ❌ |
| Standard workloads | ⚠️ TPC-C-like orders | ❌ | ✅ full TPC-C, OLTP suites | ❌ | ❌ | ❌ |
| Remote load generator | ✅ over SSH | ✅ | ✅ | n/a | n/a | n/a |
| Proof of lost writes | ✅ ledger | ✅ strongest (history checkers) | ❌ | ❌ | ❌ | ❌ |
| Stale reads | ✅ replica visibility delay | ✅ full anomaly checks | ❌ | ❌ | ❌ | ❌ |
| Dashboards + PDF reports | ✅ | ❌ | ⚠️ text | ⚠️ | ✅ | ✅ |
| Repeats + comparison charts | ✅ | ✅ | ⚠️ manual | ❌ | ❌ | ❌ |
| Human gates on risky steps | ✅ | n/a | n/a | ⚠️ | ✅ | ⚠️ |
| Local VM lab | ✅ Lima (no MySQL on ARM) | ✅ Docker/LXC | n/a | ✅ kind | ❌ | ✅ Vagrant |
| CI | ✅ tests, lint, syntax | ✅ | ✅ | ✅ | n/a | ✅ |
| Learning curve | low: make + YAML | high: Clojure | low | medium: Kubernetes | low: GUI, commercial | low |

## Ratings

| Area | Feature | Tested | Missing for 10 (feature) |
|---|---|---|---|
| Fault injection | 9.5 | 7 | one-way packet loss, process OOM |
| Load | 9 | 7 | full TPC-C; open-model arrivals with pooled connections |
| Metrics / reports | 9.5 | 8 | |
| Coverage | 9 | 5 | MySQL on the local lab (no ARM64 packages), MaxScale |
| Correctness | 9 | 8 | anomaly checks beyond visibility (write skew, lost updates) |
| **Overall** | **9.2** | **7** | |

Tested ratings rise with the smoke run (⏳ rows) and with 3 x86_64 servers running `benchmark/suites/ha-standard.yml`.
