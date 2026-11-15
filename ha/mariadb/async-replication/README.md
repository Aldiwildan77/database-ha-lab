# MariaDB asynchronous primary–replica (GTID)

## Architecture

```text
        writes ──▶ primary (read_only=0)
                     │ binlog (MariaDB GTID, MASTER_USE_GTID=current_pos, TLS)
          ┌──────────┴──────────┐
          ▼                     ▼
     replica (read_only=1)  replica (read_only=1)   … any number
```

- One primary and N ≥ 1 replicas.
- MariaDB GTIDs are `domain-server-sequence`, not MySQL UUID sets. Positions are compared per domain.
- Replicas use `MASTER_USE_GTID=current_pos`. A node that was primary earlier has its own writes in the binlog, and `current_pos` includes them.
- The replication account `repl` exists on every node. It is created with `sql_log_bin=0`, so any node can become primary without errant GTIDs.
- Replicas are `read_only=1`. Root bypasses it through `READ ONLY ADMIN`. The role (`read_only`, semi-sync flags) is also written to `/etc/mysql/mariadb.conf.d/zz-ha-lab-role.cnf`, so it survives a restart.
- Failover is **manual**. Nothing promotes a replica automatically.

## Durability

The primary acknowledges a commit as soon as it is durable locally. Replicas receive it later. **If the primary dies, every committed transaction that no replica has received is lost.** The benchmark reports this as RPO: acknowledged writes missing on the final primary.

## Deploy

```bash
# config/ha.yml: db_engine: mariadb, ha_provider: async-replication
# config/secrets.yml: db_replication_password (>= 16 chars)
make provision          # MariaDB + accounts on every node
make ha HA=async-replication
make topology
```

Re-running `make ha` is idempotent. Deployment refuses in these cases:

| Condition | What happens |
|---|---|
| A replica is ahead of the primary in any GTID domain (errant transactions) | Refused, with both positions shown |
| The primary's binlog no longer has the replica's position (error 1236) | Refused; the replica diverged |
| A different strategy is already deployed (`/etc/mysql/.ha-lab-provider`) | Refused |

## Health checks

`make topology` passes only when all of these hold:
- exactly one node is writable;
- every other node is read-only;
- every read-only node replicates from that writable node, with IO and SQL threads running.

## Failover runbook (primary lost)

1. Confirm the primary is really gone. Check `make topology` and Grafana → *Replication & HA*. If it is reachable but misbehaving, use a switchover instead.
2. Run `make promote`. The playbook:
   - waits up to 120 s for every replica to apply what it received (`gtid_current_pos`);
   - picks a replica that is at or ahead of every other candidate in every GTID domain, or the one you pass in `NEW_PRIMARY` if it qualifies;
   - promotes it: `STOP SLAVE`, `RESET SLAVE ALL`, `read_only=0`;
   - repoints the remaining replicas to it.
3. Point clients at the new primary. The benchmark's `ha` endpoint mode finds the writable node automatically.
4. Events are written to `local/state/failover-*.json`.

If the old primary is still running (a network partition), `make promote` refuses. Pass `FORCE=1` only after you are sure clients cannot reach it. The playbook then sets `read_only=1` on the old primary if it can still reach it, and persists it.

## Planned switchover

```bash
make switchover NEW_PRIMARY=db-02
```

1. The current primary is fenced with `read_only=1`.
2. Every replica waits until it has applied the fenced primary's full GTID position.
3. The new primary is promoted, and the old primary becomes a replica.

No acknowledged transaction is lost. If no candidate qualifies, the old primary is unfenced.

## Recovery runbook (old primary returns)

```bash
make recover NODE=db-01
```

1. Starts MariaDB on the node.
2. Checks that the node is not ahead of the current primary in any GTID domain.
3. Makes it read-only and replicates from the current primary.

If the node holds transactions that never reached a replica, recovery stops. Those transactions are the data lost in the failover. Inspect them with `mariadb-binlog`, then rebuild the node (`make destroy DATA=1` for that node, or a backup restore).

## Monitoring

- **Dashboards:** *Replication & HA* and *Failover & Recovery* use engine-neutral metrics. There is no MariaDB-specific server dashboard yet.
- **Lag:** `replication_lag_seconds` is MariaDB's `Seconds_Behind_Master`. It is the replica clock minus the original commit time of the event being applied.

## Known limitations

- There is no automatic failover. RTO includes human or automation reaction time. The benchmark simulates this with `failover.manual_promotion_delay_seconds`.
- There is no automatic fencing. A partitioned old primary keeps accepting writes from clients that can still reach it.
- Multi-source replication and more than one GTID domain in use at once have not been tested.
- Lag metrics compare clocks on different hosts. See the clock offset in each report.
