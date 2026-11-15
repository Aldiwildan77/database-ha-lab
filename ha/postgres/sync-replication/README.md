# PostgreSQL synchronous replication

## Architecture

The topology is the same as [streaming-replication](../streaming-replication/README.md). The difference: the primary waits for standby acknowledgements before a commit returns.

The primary gets `conf.d/zzz-ha-lab-sync.conf`:

```text
synchronous_standby_names = 'ANY <num_sync> ("db-02", "db-03")'   # every other node, by application_name
synchronous_commit = '<commit_level>'
```

The file is written on every node and lists the node's peers, so the setting follows the primary after a switchover or failover. It only takes effect on the node that is primary.

Settings in `config/ha.yml` → `sync_replication`:

| Setting | Default | Meaning |
|---|---|---|
| `num_sync` | 1 | How many standbys must acknowledge each commit (quorum, `ANY n`) |
| `commit_level` | `on` | `remote_write`: standby wrote the WAL (not flushed). `on`: standby flushed it. `remote_apply`: standby replayed it, so reads there see the commit. |

## Durability: what it does and does not guarantee

- **Acknowledged commits are on `num_sync` standbys.** If the primary dies, a standby that acknowledged holds every acknowledged commit. `make promote` picks the most advanced standby, so no acknowledged commit is lost.
- **Commits wait instead of falling back.** Unlike MySQL/MariaDB semi-sync, there is no timeout. With fewer than `num_sync` reachable standbys, every commit waits. This is the price of no data loss.
- **A cancelled wait is not a rollback.** If a client cancels a waiting commit, the transaction is already committed locally and becomes visible, but no standby may have it.
- With `remote_write`, a simultaneous crash of the primary and the standby's OS can still lose the commit.

The benchmark measures RPO directly; do not assume 0.

## Deploy, failover, recovery

```bash
make ha HA=sync-replication
make switchover NEW_PRIMARY=db-02
make promote
make recover NODE=db-01
```

The runbooks are the streaming-replication runbooks. Deployment waits until `num_sync` standbys report `sync` or `quorum` in `pg_stat_replication`.

## Health and monitoring

`make topology` fails when the primary has fewer synchronous standbys than `num_sync`, because commits are then blocked.

| Metric | What it shows |
|---|---|
| `ha_lab_sync_standbys` | Standbys in `sync` or `quorum` state on the primary |
| `ha_lab_streaming_standbys` | Standbys streaming at all |
| `replication_lag_seconds` | Standby replay lag |

Compare `commit_duration_seconds{side="server"}` with streaming-replication runs to see the commit-latency cost.

## Known limitations

- There is no automatic failover. This uses the same manual process as streaming replication.
- Losing more than `nodes - 1 - num_sync` standbys blocks all writes until one returns or `num_sync` is lowered.
- Each commit pays for at least one network round trip to the `num_sync`-th fastest standby. Across data centers, that dominates commit latency.
