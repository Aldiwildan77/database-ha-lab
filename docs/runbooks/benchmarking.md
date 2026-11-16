# Runbook: benchmarks and failover experiments

## One-time setup

```bash
make jmeter-install     # JMeter 5.6.3, the engine's JDBC driver and plugins into local/tools (Java 8+)
make monitoring         # Prometheus, Grafana, local client probe
```

Plugins installed by `jmeter/install.sh`, available to any plan you write or open in the JMeter GUI:

| Plugin | Adds |
|---|---|
| Custom Thread Groups (`jpgc-casutg`) | Concurrency, Arrivals, Free-Form Arrivals and Ultimate thread groups (step load, fixed arrival rate) |
| Throughput Shaping Timer (`jpgc-tst`) | A requests-per-second profile over time |
| Dummy Sampler (`jpgc-dummy`) | A sampler that needs no database, for testing plans and the report pipeline |
| Plugins Manager | Options → Plugins Manager in the GUI, or `local/tools/jmeter/bin/PluginsManagerCMD.sh`, to add other plugins |

The generated `jmeter/scenarios/*.jmx` use core JMeter elements only. Plugins added through the Plugins Manager are not pinned or checksum-verified; add them to `jmeter/install.sh` to keep a lab reproducible.

Also check these settings:
- `db_benchmark_password` is set. `make provision` creates the `bench` account from it.
- `config/benchmark.yml` holds the endpoint, dataset and load defaults.

## Run a scenario

```bash
make benchmark SCENARIO=mixed-workload
make benchmark SCENARIO=read-heavy ENDPOINT=primary J="threads=32 duration=600"
make benchmark SCENARIO=spike-test J="spike_threads=128 spike_at=90"
```

| `ENDPOINT` | Type | What it measures |
|---|---|---|
| `ha` (default) | HA endpoint | Client-facing HA behaviour. Uses MySQL Router on every node (innodb-cluster) or writable-node discovery across all nodes (every other strategy, on every engine). |
| `primary` | direct | The node that is currently writable |
| `<node>` | direct | One specific node, for isolated diagnostics |

Reports label the endpoint type. Never compare direct and HA results without stating which is which.

## What a run does

1. Allocates `run_id = run-YYYYMMDD-NNN` and creates `benchmark/results/<run_id>/`.
2. Collects context with Ansible:
   - node specs, clock offset and sync state;
   - the current writable node and the endpoints.

   The benchmark password goes into a temporary `secret.properties` (mode 0600). It is deleted at the end of the run.
3. Measures the network:
   - client→node: ping, TCP connect and Tailscale path, measured locally;
   - node↔node: the `network-matrix` playbook.
4. Prepares the dataset (deterministic: same `rows` and `seed` give identical data), then waits for replicas to converge.
5. Starts `jtl_exporter.py` on :9464 (Prometheus job `benchmark`), the topology poller (every 0.5 s against each node's collector), and a Grafana annotation tagged with the `run_id`.
6. Runs JMeter. It injects and restores the fault if one is configured.
7. Waits for replication to converge, then dumps the ledger from every node.
8. Writes the results:

```text
benchmark/results/<run_id>/
├── metadata.json            run parameters, git commit, start/end, initial and final primary
├── context.json             topology, node specs, clocks, endpoints
├── network.json             client→node and node↔node measurements
├── jmeter.jtl               one row per transaction (txn_id, op, *_ns timings, served_by, err_class)
├── failover-events.json     client-clock event timeline
├── ledger/                  per-node txn_id dumps used for RPO
├── consistency.json
├── prometheus-summary.json  CPU, memory, IOPS, disk latency, lag, server commit p99, RTT
├── results.json             flat machine-readable summary
└── report.md
benchmark/results/summary.csv   one row per run, for comparing architectures
```

To rebuild a report later (for example after Prometheus was down): `make report RUN=<run_id>`.

## Failover experiment

```bash
make failover CONFIRM=yes                         # stop-db on the primary under write-heavy load
make failover CONFIRM=yes FAULT=kill-db           # SIGKILL (systemd may restart the server)
make failover CONFIRM=yes FAULT=isolate           # drop DB ports on the node's lab interface; SSH and exporters stay up
make benchmark SCENARIO=network-disruption CONFIRM=yes RUN_ARGS='--netem "delay 150ms 30ms loss 2%"'   # applied on the node's lab interface
```

**Preflight.** Before each experiment, check:
- `make mesh-check` passes;
- `make topology` is healthy;
- `make metrics-check` shows every target up;
- no other benchmark is running.

The fault is injected at one third of the duration by default (`RUN_ARGS='--fault-at 60 --fault-duration 90'`).

How the run handles the fault depends on the provider:

| Provider | Promotion | After the fault is restored |
|---|---|---|
| async-replication, semi-sync | The runner waits `manual_promotion_delay_seconds` after detection, to simulate operator reaction time, then runs `failover.yml`. Use `--no-promote` to measure the raw outage instead. | The old primary is rejoined with `recover.yml` |
| orchestrator, innodb-cluster, percona-xtradb-cluster | The runner only observes the provider's automatic failover | Not rejoined automatically. Use `make recover NODE=<old primary>` if needed. |

Safety:
- Isolation and netem schedule an automatic restore on the node through `systemd-run`, in case the runner dies.
- An interrupted run restores the fault in a `finally` block.

### Definitions used in reports

All times use the client clock.

| Metric | Definition |
|---|---|
| Detection | Fault injected → primary seen unavailable by the poller |
| Failover duration | Primary unavailable → another node writable |
| RTO | Fault injected → first successful write that started after the first failed write. 0 when no write failed. |
| RPO | Acknowledged writes (JTL `success=true`) missing from the final primary's ledger. Writes whose COMMIT outcome is unknown are counted separately. |
| Consistency errors | Lost acknowledged writes, plus writes the client saw fail that exist anyway, plus duplicates. Also reported: nodes whose ledger differs from the primary, and whether the account balance checksum matches. |
| Write and read availability | Successful / attempted requests; also the share of seconds that had at least one success |
| Replication convergence | Fault → all reachable nodes report the same replication position (`ha_lab_replication_position`: GTID count on MySQL/MariaDB, WAL LSN on PostgreSQL) |
| Recovery duration | Fault → every node healthy again (writable, or replicating with both threads, or ONLINE in the group, or wsrep ready) |

The report states only what was measured. It never claims zero downtime or zero data loss beyond the run's own numbers.
