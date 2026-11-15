# Metrics

The full catalogue is in [`metrics/schema/metrics.yml`](../../metrics/schema/metrics.yml). This page explains how the levels relate and what each one can and cannot tell you.

## Sources

| Job | Runs on | Port | Produces |
|---|---|---|---|
| `node` | every node | 9100 | Host resources, plus `network_*` from the node-to-node probe (textfile, every 30 s) |
| `mysqld` | every node | 9104 | mysqld_exporter: global status, InnoDB, replication, Group Replication, wsrep |
| `halab` | every node | 9106 | `metrics/collectors/ha_lab_collector.py`: server-side histograms, replication apply, topology state |
| `benchmark` | this machine | 9464 | `benchmark/runner/jtl_exporter.py`: Level 0 and client-side Level 1/2, while a benchmark runs |
| `local_probe` | this machine | 9465 | `metrics/collectors/local_probe.py`: client-to-node RTT, loss, TCP connect, Tailscale path |

## The levels

- **Level 0** is the client's view: `ha_transaction_duration_seconds` is measured with `System.nanoTime()` around the whole transaction inside the JMeter sampler. It is never computed by adding components.
- **Level 1 and Level 2** split that time into components. Each one is measured independently:

  ```text
  client  ── connect ──┬─ statements ──────────────┬─ COMMIT ─────────────────┐
                       │   (network + server exec) │  (network + server commit)│
  server               │   statement exec          │  redo/binlog fsync        │
                       │   lock waits              │  + semi-sync ACK wait     │
                       │                           │  + GR / Galera certify    │
  replica                                           └─ relay → apply (later, async)
  ```

  The components overlap. Client statement time contains server execution and network RTT. Server commit time contains the fsync and any replication acknowledgement. Asynchronous apply happens after the client already got its acknowledgement. The dashboards and reports never sum them.
- **Level 3** is node_exporter. Disk latency there is a mean derived from `/proc/diskstats`, not a distribution.
- **Level 4** is HA state. The collector exposes the same `ha_lab_*` names for every provider, so failover detection works the same way regardless of architecture.

## Timer units and clocks

- Performance Schema timers are picoseconds. The collector divides by 1e12.
- `Innodb_row_lock_time` is in milliseconds.
- Durations on the client use monotonic timers.
- Event timestamps (fault injected, primary unavailable, new primary writable) are all taken on the client clock. That makes RTO and detection times free of cross-host clock error.
- Two metrics subtract timestamps from different hosts: `replication_lag_seconds` and `replication_end_to_end_delay_seconds`. Both compare the source's original commit time with the replica's clock. Their uncertainty is at least the chrony offset that each report records.

## Not available, and why

| Requested | Status | Alternative |
|---|---|---|
| `connection_pool_wait_seconds` | Not applicable. Each JMeter thread holds one connection, so there is no pool. | `connection_duration_seconds` on reconnect |
| `network_transfer_duration_seconds` | Cannot be separated from statement time at the client | RTT histograms, plus iperf3 throughput (`make network-matrix IPERF=1`) |
| Lock wait histogram | MySQL exposes only a sum and a count | `database_lock_wait_seconds_total / database_lock_waits_total` |
| fsync / disk I/O histograms | Performance Schema and diskstats expose sums only | Derived means. eBPF `biolatency` is out of scope. |
| `replication_errors_total` | MySQL keeps only the last error | `replication_last_error_number` gauge |
| Group Replication flow-control time | Not in Performance Schema | Watch applier/certification queue growth |
| MySQL Router metrics | Router has a REST API but no Prometheus endpoint | Client-side `served_by` in the JTL; Router logs |
