# Semi-synchronous replication

## Architecture

The topology is the same as [async-replication](../async-replication/README.md). The differences:

- Both `rpl_semi_sync_source` and `rpl_semi_sync_replica` are installed on **every** node, so any node can be promoted.
- The primary has `rpl_semi_sync_source_enabled=ON`. Replicas have `rpl_semi_sync_replica_enabled=ON`.

Settings in `config/ha.yml` → `semi_sync`:

| Setting | Default | Meaning |
|---|---|---|
| `wait_for_replica_count` | 1 | How many replicas must acknowledge receipt (relay log write) before the commit returns |
| `timeout_ms` | 10000 | How long to wait for that acknowledgement. After this, the primary **silently falls back to asynchronous** replication until a replica catches up. |
| `wait_point` | `AFTER_SYNC` | Lossless mode. The transaction is not visible to other sessions until it is acknowledged. `AFTER_COMMIT` makes it visible before the acknowledgement. |

## Durability: what semi-sync does and does not guarantee

- **Receipt, not apply.** The acknowledgement means "written to a replica's relay log". It does not mean "applied". A promoted replica must still apply its relay log, which `make promote` waits for.
- **Fallback is silent.** After a timeout, commits continue with no replica acknowledgement until `Rpl_semi_sync_source_status` goes back to `ON`. A primary failure in that window loses data exactly like async replication.
- **Writes can stall.** With `wait_for_replica_count` > 0 and all semi-sync replicas down, every commit waits `timeout_ms` before it falls back.

So **semi-sync does not by itself guarantee zero data loss.** The benchmark measures RPO directly.

## Deploy, failover, recovery

```bash
make ha HA=semi-sync
make switchover NEW_PRIMARY=mysql-02   # semi-sync source/replica roles move with the primary
make promote
make recover NODE=mysql-01
```

The runbooks are the async-replication runbooks. The playbooks also move the semi-sync roles: the new primary gets source ON and replica OFF, and every other node gets the reverse.

## Health and monitoring

`make topology` prints `semi-sync active` or a warning when the primary has fallen back to async.

The *Replication & HA* dashboard shows:

| Metric | What it shows |
|---|---|
| `mysql_global_status_rpl_semi_sync_source_yes_tx` | Acknowledged commits |
| `mysql_global_status_rpl_semi_sync_source_no_tx` | Commits that fell back to async |
| `rpl_semi_sync_source_tx_avg_wait_time` | Commit latency added by waiting for the acknowledgement (shown in seconds) |
| `ha_lab_semi_sync_source_active` | Whether the primary is currently in semi-sync mode |

Compare `commit_duration_seconds{side="server"}` with async runs to see the commit-latency cost.

## Known limitations

- There is no automatic failover. This uses the same manual process as async.
- Each commit pays for at least one network round trip to the nearest semi-sync replica. Across data centers, that dominates commit latency.
