# Capabilities

Two separate ratings:
- **Feature**: what the code can do.
- **Tested**: what has run on real servers (2 nodes so far: lab-hanif, lab-ivan).

✅ yes · ⚠️ partial · ❌ no

## Catalog

| Area | Capability | Feature | Tested |
|---|---|---|---|
| Engines | MySQL 8.4: async, semi-sync | ✅ | ✅ deploy, failover, rebuild, chaos |
| | MySQL: Orchestrator, InnoDB Cluster, Percona XtraDB Cluster | ✅ | ❌ needs 3 nodes |
| | MariaDB: async, semi-sync, Galera (MaxScale planned) | ✅ | ❌ |
| | PostgreSQL 16-18: streaming, sync | ✅ | ⚠️ deploy + degrade runs; no failover yet |
| | PostgreSQL: Patroni + etcd | ✅ | ❌ needs 3 nodes |
| Infrastructure | Provision with Ansible, plan-host dry run, validate, destroy (data purge optional) | ✅ | ✅ |
| | Networks per node: Tailscale, VPN, private LAN, mixed | ✅ | ⚠️ Tailscale + WireGuard |
| | Mesh, network matrix (RTT, loss, iperf) | ✅ | ✅ |
| | Config dir switch (`LAB_CONFIG_DIR`) | ✅ | ✅ |
| | Local VM lab (Lima, aarch64: MariaDB, PostgreSQL) | ✅ | ⚠️ 1 VM, Ansible reachable |
| Operations | Switchover, promote, recover (CLONE / pg_rewind), topology | ✅ | ⚠️ MySQL only |
| | Safety: confirmations, "replica holds data" guard, no second primary on restore, fencing of a frozen primary | ✅ | ⚠️ guards seen firing; fencing untested |
| Faults | stop-db, kill-db, isolate (node-to-node), netem | ✅ | ✅ under load |
| | degrade (CPU, disk, memory + netem) | ✅ | ✅ under load |
| | freeze (SIGSTOP), slow-disk (cgroup IO cap), partition-client | ✅ | ⚠️ inject/restore verified, not under load |
| | Pre-armed start (never over the broken link), auto-restore timers, active-at-restore check | ✅ | ✅ |
| | Chaos plan: several faults on a schedule in one run | ✅ | ⚠️ the script it replaces ran; `make chaos` not yet |
| Load | 11 scenarios: steady, read/write heavy, spike, soak, wave, failover, network | ✅ | ✅ (soak not run) |
| | Fixed request rate (load does not drop when latency rises) | ✅ | ⚠️ verified locally with JMeter |
| | Endpoints: HA, primary, single node; JDBC driver per engine | ✅ | ✅ |
| | Suites with repeats | ✅ | ❌ not run end to end |
| Correctness | Per-write ledger: lost, phantom, duplicate, unknown-outcome writes on every node | ✅ | ✅ caught real loss (run 022) |
| | Account balance checksum across nodes | ✅ | ✅ |
| Detection | Primary down = not answering at protocol level for 3 polls, re-checked before promotion | ✅ | ⚠️ previous TCP-less version tested |
| Metrics | Prometheus + Grafana, node/engine exporters, lab collector, client probe, live JMeter metrics | ✅ | ✅ |
| Reports | report.md/html, timeline chart, Grafana screenshots (all or filtered), PDF with IP redaction | ✅ | ✅ |
| | Run comparison: median, min-max, worst-case loss, grouped by HA settings | ✅ | ✅ on past runs |

## Compared with other platforms (feature level)

Other columns come from public documentation and general knowledge, not from runs in this lab.

| Feature | Ours | Jepsen | sysbench / HammerDB | Chaos Mesh / Litmus | ClusterControl | autobase / Pigsty |
|---|---|---|---|---|---|---|
| Deploy HA on VMs / bare metal | ✅ 3 engines, 11 strategies | ⚠️ test setups | ❌ | ❌ | ✅ | ✅ PG only |
| One config for engine + strategy | ✅ | ❌ code per DB | ❌ | ❌ | ✅ GUI | ⚠️ |
| Crash / stop faults | ✅ | ✅ | ❌ | ✅ | ⚠️ manual | ❌ |
| Network partitions | ✅ node-to-node, client-side | ✅ richest | ❌ | ✅ | ❌ | ❌ |
| Gray failures (hung, slow disk, degraded) | ✅ | ⚠️ pause | ❌ | ✅ | ❌ | ❌ |
| Clock skew, disk full | ❌ | ✅ clock | ❌ | ✅ | ❌ | ❌ |
| Fault schedules | ✅ | ✅ generators | ❌ | ✅ workflows | ❌ | ❌ |
| Load: steady, spike, wave, fixed rate | ✅ | ⚠️ light | ✅ best (TPC-C, OLTP) | ❌ | ❌ | ❌ |
| Standard workloads (TPC-C) | ❌ one transaction mix | ❌ | ✅ | ❌ | ❌ | ❌ |
| Proof of lost writes | ✅ ledger | ✅ strongest (history checkers) | ❌ | ❌ | ❌ | ❌ |
| Dashboards + PDF reports | ✅ | ❌ | ⚠️ text | ⚠️ | ✅ | ✅ |
| Repeats + comparison | ✅ | ✅ | ⚠️ manual | ❌ | ❌ | ❌ |
| Human gates on risky steps | ✅ | n/a | n/a | ⚠️ | ✅ | ⚠️ |
| Local VM lab | ✅ Lima | ✅ Docker/LXC | n/a | ✅ kind | ❌ | ✅ Vagrant |
| Learning curve | low: make + YAML | high: Clojure | low | medium: Kubernetes | low: GUI, commercial | low |

## Ratings

| Area | Feature | Tested | Missing for 10 (feature) |
|---|---|---|---|
| Fault injection | 9 | 7 | clock skew, disk full |
| Load | 8 | 6 | standard workloads (TPC-C-like), load generator inside the DC as a config option |
| Metrics / reports | 8 | 7 | charts in the comparison report |
| Coverage | 8 | 4 | (tested) 3-node strategies, MariaDB, PostgreSQL failover |
| **Overall** | **8.5** | **6** | |
