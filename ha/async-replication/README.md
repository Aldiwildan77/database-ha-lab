# Asynchronous primary–replica (GTID)

## Architecture

```text
        writes ──▶ primary (read_only=OFF)
                     │ binlog (GTID, auto-position, TLS)
          ┌──────────┴──────────┐
          ▼                     ▼
     replica (super_read_only)  replica (super_read_only)   … any number
```

- One primary and N ≥ 1 replicas, with `SOURCE_AUTO_POSITION=1`.
- The replication account `repl` exists on every node. It is created with `sql_log_bin=0`, so any node can become primary without errant GTIDs.
- Replicas are `read_only` and `super_read_only`, both persisted with `SET PERSIST`.
- Failover is **manual**. Nothing promotes a replica automatically.

## Durability

The primary acknowledges a commit as soon as it is durable locally (`sync_binlog=1`, `innodb_flush_log_at_trx_commit=1`). Replicas receive it later. **If the primary dies, every committed transaction that no replica has received is lost.** The benchmark reports this as RPO: acknowledged writes missing on the final primary.

## Deploy

```bash
# config/ha.yml: ha_provider: async-replication; config/secrets.yml: db_replication_password
make provision          # MySQL + accounts on every node
make ha HA=async-replication
make topology
```

Re-running `make ha` is idempotent: a second run reports `changed=0`. Deployment refuses in these cases:

| Condition | What happens |
|---|---|
| A replica has transactions the primary lacks (errant GTIDs) | Refused, with the GTID set shown. It never replicates over divergent data. |
| The intended primary is itself replicating | Refused |
| A different provider is already deployed (`/etc/mysql/.ha-lab-provider`) | Refused |

## Health checks

`make topology` passes only when all of these hold:
- exactly one node is writable;
- every other node is read-only;
- every read-only node replicates from that writable node, with IO and SQL threads ON.

## Failover runbook (primary lost)

1. Confirm the primary is really gone. Check `make topology` and Grafana → *Replication & HA*. If it is reachable but misbehaving, use a switchover instead.
2. Run `make promote`. The playbook:
   - waits up to 120 s for every replica to apply its relay log;
   - picks the replica whose GTID set contains all the others (or the one you pass in `NEW_PRIMARY`, if that replica qualifies);
   - promotes it: `STOP REPLICA`, `RESET REPLICA ALL`, `read_only=OFF`;
   - repoints the remaining replicas to it.
3. Point clients at the new primary. The benchmark's `ha` endpoint mode finds the writable node automatically.
4. Events are written to `local/state/failover-*.json`.

If the old primary is still running (a network partition), `make promote` refuses. Pass `FORCE=1` only after you are sure clients cannot reach it. The playbook then sets `super_read_only` on the old primary if it can still reach it over SSH.

## Planned switchover

```bash
make switchover NEW_PRIMARY=mysql-02
```

1. The current primary is fenced with `super_read_only`.
2. Every replica waits until it has the fenced primary's full GTID set.
3. The new primary is promoted, and the old primary becomes a replica.

No acknowledged transaction is lost. If anything fails before the promotion, the old primary is unfenced automatically.

## Recovery runbook (old primary returns)

```bash
make recover NODE=mysql-01
```

1. Starts MySQL on the node.
2. Checks that its GTID set is a subset of the current primary's.
3. Makes it read-only and replicates from the current primary.

If the node holds transactions that never reached a replica (errant GTIDs), recovery stops. Those transactions are the data lost in the failover. Inspect them with `mysqlbinlog`, then rebuild the node: `make destroy DATA=1` for that node, or a clone/backup restore.

## Monitoring

- **Dashboards:** *Replication & HA* (lag, threads, relay backlog, GTID convergence) and *Failover & Recovery*.
- **Lag:**
  - `replication_lag_seconds` is the applier-based lag.
  - `mysql_slave_status_seconds_behind_source` is MySQL's coarse lag. It reads NULL when the SQL thread is stopped.

## Known limitations

- There is no automatic failover. RTO includes human or automation reaction time. The benchmark simulates this with `failover.manual_promotion_delay_seconds`.
- There is no automatic fencing. A partitioned old primary keeps accepting writes from clients that can still reach it.
- Lag metrics compare clocks on different hosts. See the clock offset in each report.
