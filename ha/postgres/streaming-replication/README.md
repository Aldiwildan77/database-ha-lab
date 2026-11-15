# PostgreSQL streaming replication (physical, asynchronous)

## Architecture

```text
        writes ──▶ primary
                     │ WAL stream (walsender → walreceiver, TLS, scram-sha-256)
          ┌──────────┴──────────┐
          ▼                     ▼
     hot standby            hot standby        … any number
```

- One PGDG cluster named `halab` per node (`/etc/postgresql/<version>/halab`). The lab never touches other clusters on the host.
- `make provision` creates every node standalone. `make ha` turns the non-primary nodes into standbys: a standalone node is replaced with a `pg_basebackup` of the primary, then streams from it.
- Standbys are hot standbys (read-only queries allowed). `primary_conninfo` is set with `ALTER SYSTEM`; `recovery_target_timeline=latest` lets them follow a new primary's timeline.
- Accounts: `repl` (replication), `rewind` (pg_rewind source access), `bench` (benchmark). Accounts are created on the primary and reach the standbys through replication.
- `wal_log_hints=on`, so a failed primary can be rewound instead of rebuilt.
- No replication slots. `wal_keep_size=1GB` bounds how far a standby can fall behind before it needs a rebuild.
- Failover is **manual**. Nothing promotes a standby automatically.

## Durability

The primary acknowledges a commit once its WAL is flushed locally (`synchronous_commit=on` with no synchronous standbys). Standbys receive it later. **If the primary dies, every committed transaction whose WAL no standby received is lost.** The benchmark reports this as RPO.

## Deploy

```bash
# config/ha.yml: db_engine: postgres, ha_provider: streaming-replication
# config/secrets.yml: db_replication_password (>= 16 chars), db_admin_password (for pg_rewind)
make provision
make ha HA=streaming-replication
make topology
```

Re-running `make ha` is idempotent. Deployment refuses in these cases:

| Condition | What happens |
|---|---|
| A standalone node holds databases or tables beyond the lab's empty benchmark database | Refused. Pass `ARGS="-e postgres_rebuild_replica=true"` to replace its data with a copy of the primary. |
| Data checksums and `wal_log_hints` are both off | Refused at provisioning; pg_rewind would not work |
| A different strategy is already deployed (`/etc/postgresql/.ha-lab-provider`) | Refused |

## Health checks

`make topology` passes only when all of these hold:
- exactly one node is out of recovery (writable);
- every other node is in recovery;
- every standby's walreceiver streams from that writable node.

## Failover runbook (primary lost)

1. Confirm the primary is really gone. Check `make topology` and Grafana → *Replication & HA*. If it is reachable, use a switchover instead.
2. Run `make promote`. The playbook:
   - waits up to 120 s for every standby to replay everything it received;
   - picks the standby with the highest replay LSN, or the one you pass in `NEW_PRIMARY` if no other standby is ahead of it;
   - promotes it with `pg_promote()`, clears its `primary_conninfo` and runs a `CHECKPOINT` (pg_rewind needs one on the new timeline);
   - repoints the remaining standbys to it. They follow the new timeline.
3. Point clients at the new primary. The benchmark's `ha` endpoint mode finds the writable node automatically.
4. Events are written to `local/state/failover-*.json`.

If the old primary is still running, `make promote` refuses. Pass `FORCE=1` only after you are sure clients cannot reach it. The playbook then stops the old primary (`pg_ctlcluster stop -m fast`) if it can reach it.

## Planned switchover

```bash
make switchover NEW_PRIMARY=db-02
```

1. The current primary is stopped cleanly. A fast shutdown makes its walsenders ship all WAL, including the shutdown checkpoint, to the standbys.
2. Every standby waits until it has replayed past that checkpoint. A standby that never received it is not eligible.
3. The new primary is promoted. The old primary starts again as a standby of it.

No acknowledged transaction is lost. If no candidate qualifies, the old primary is started again as primary.

## Recovery runbook (old primary returns)

```bash
make recover NODE=db-01
```

- A node that comes back as a standby is repointed to the current primary.
- A node that comes back as a primary (the old primary after an unplanned failover) is stopped, rewound onto the new timeline with `pg_rewind`, and started as a standby. pg_rewind discards the transactions it had that never reached a standby; those are the data lost in the failover.
- If the rewind fails (`db_admin_password` unset, or WAL no longer available), recovery stops and says why. To replace the node's data with a fresh base backup instead, run:

  ```bash
  make recover NODE=db-01 ARGS="-e postgres_rebuild=true"
  ```

## Monitoring

- **Dashboards:** *Replication & HA* and *Failover & Recovery* use engine-neutral metrics. There is no PostgreSQL-specific server dashboard yet; `postgres_exporter` metrics (`pg_*`) are in Prometheus.
- **Lag:** `replication_lag_seconds` is measured on the standby: `now()` minus the commit time of the last replayed transaction. It reads 0 when the primary is idle.
- `ha_lab_streaming_standbys` (primary side) and `ha_lab_postgres_timeline` show topology changes.

## Known limitations

- There is no automatic failover. RTO includes human or automation reaction time. The benchmark simulates this with `failover.manual_promotion_delay_seconds`.
- Fencing is "stop the old primary". A partitioned old primary that Ansible cannot reach keeps accepting writes from clients that can still reach it.
- No replication slots: a standby down for longer than `wal_keep_size` of WAL needs a rebuild.
- Cascading replication and logical replication are not covered.
- Lag metrics compare clocks on different hosts. See the clock offset in each report.
