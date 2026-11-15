# MariaDB semi-synchronous replication

## Architecture

The topology is the same as [async-replication](../async-replication/README.md). The differences:

- Semi-sync is built into MariaDB; no plugin is installed.
- Both sides are configured on **every** node, so any node can be promoted.
- The primary has `rpl_semi_sync_master_enabled=ON`. Replicas have `rpl_semi_sync_slave_enabled=ON`.
- A replica negotiates semi-sync only when its IO thread connects. The role restarts the IO thread when the setting changes.

Settings in `config/ha.yml` → `semi_sync`:

| Setting | Default | Meaning |
|---|---|---|
| `wait_for_replica_count` | 1 | Must be 1. MariaDB always waits for exactly one replica; any other value is refused. |
| `timeout_ms` | 10000 | `rpl_semi_sync_master_timeout`. After this, the primary **silently falls back to asynchronous** replication until a replica catches up. |
| `wait_point` | `AFTER_SYNC` | `rpl_semi_sync_master_wait_point`. `AFTER_SYNC` makes the transaction visible to other sessions only after the acknowledgement. `AFTER_COMMIT` makes it visible before. |

## Durability: what semi-sync does and does not guarantee

- **Receipt, not apply.** The acknowledgement means "written to a replica's relay log". A promoted replica must still apply it, which `make promote` waits for.
- **Fallback is silent.** After a timeout, commits continue with no acknowledgement until `Rpl_semi_sync_master_status` goes back to `ON`. A primary failure in that window loses data exactly like async replication.
- **Writes can stall.** With every replica down, each commit waits `timeout_ms` before it falls back.

So **semi-sync does not by itself guarantee zero data loss.** The benchmark measures RPO directly.

## Deploy, failover, recovery

```bash
make ha HA=semi-sync
make switchover NEW_PRIMARY=db-02   # master/slave semi-sync flags move with the primary
make promote
make recover NODE=db-01
```

The runbooks are the async-replication runbooks. The playbooks also move the semi-sync flags: the new primary gets master ON and slave OFF, and every other reachable node gets the reverse. An unreachable old primary keeps master ON in its persisted config until `make recover`.

## Health and monitoring

`make topology` prints `semi-sync active` or a warning when the primary has fallen back to async.

| Metric | What it shows |
|---|---|
| `ha_lab_semi_sync_source_active` | Whether the primary is currently in semi-sync mode (`Rpl_semi_sync_master_status`) |
| `ha_lab_semi_sync_replicas` | Semi-sync replicas connected to the primary (`Rpl_semi_sync_master_clients`) |
| `replication_lag_seconds` | Replica lag (`Seconds_Behind_Master`) |

Compare `commit_duration_seconds{side="server"}` with async runs to see the commit-latency cost.

## Known limitations

- There is no automatic failover. This uses the same manual process as async.
- Only one replica acknowledgement can be required.
- Each commit pays for at least one network round trip to the nearest semi-sync replica. Across data centers, that dominates commit latency.
