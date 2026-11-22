# HA scenarios

One folder per engine and strategy: `ha/<engine>/<strategy>/`. Each documents the design, deployment, health checks, failover and recovery runbooks, monitoring and known limitations. The Ansible side lives in `ansible/engines/<engine>/` (roles and hooks). The core playbooks are the same for every engine.

Pick the pair in `config/ha.yml`:

```yaml
db_engine: postgres                # mysql | mariadb | postgres
ha_provider: streaming-replication # one of the engine's strategies below
```

## MySQL

| Strategy | Failover | Commit acknowledged when | Data loss on primary failure |
|---|---|---|---|
| [async-replication](mysql/async-replication/README.md) | manual (`make promote`) | the primary has written it locally | possible: anything not yet received by a replica |
| [semi-sync](mysql/semi-sync/README.md) | manual | ≥ N replicas received it (relay log), or the timeout expired | possible after a timeout fallback |
| [orchestrator](mysql/orchestrator/README.md) | automatic (raft-coordinated) | same as async | same as async; orchestrator moves topology, it adds no durability |
| [innodb-cluster](mysql/innodb-cluster/README.md) | automatic (group election) | a majority received it and certification passed | not for majority-acknowledged commits while quorum holds |
| [percona-xtradb-cluster](mysql/percona-xtradb-cluster/README.md) | none needed (multi-primary) | every node certified it | not while a primary component exists; a lost quorum blocks writes |

## MariaDB

| Strategy | Failover | Commit acknowledged when | Data loss on primary failure |
|---|---|---|---|
| [async-replication](mariadb/async-replication/README.md) | manual (`make promote`) | the primary has written it locally | possible: anything not yet received by a replica |
| [semi-sync](mariadb/semi-sync/README.md) | manual | one replica received it, or the timeout expired | possible after a timeout fallback |
| [galera](mariadb/galera/README.md) | none needed (multi-primary) | every node certified it | not while a primary component exists; a lost quorum blocks writes |
| `maxscale` | planned | | |

## PostgreSQL

| Strategy | Failover | Commit acknowledged when | Data loss on primary failure |
|---|---|---|---|
| [streaming-replication](postgres/streaming-replication/README.md) | manual (`make promote`) | the primary has flushed its WAL locally | possible: WAL not yet received by a standby |
| [sync-replication](postgres/sync-replication/README.md) | manual | `num_sync` standbys acknowledged it (`synchronous_commit` level) | possible when every acknowledging standby is lost with the primary; commits wait instead of falling back |
| [patroni](postgres/patroni/README.md) | automatic (Patroni, etcd DCS) | the leader flushed its WAL; with `synchronous_mode`, the synchronous standby too | async: WAL no replica received (a replica more than `maximum_lag_on_failover` behind is not promoted); `synchronous_mode`: none |

Planned strategies are refused by `make validate-config` until their roles exist.

These guarantees differ. Benchmark reports state the **measured** RPO and RTO for each run, never an assumed one.

## Common commands

The same targets drive every engine. The engine hooks do the engine-specific work.

```bash
make provision                 # server packages, accounts, exporters
make ha HA=<strategy>          # build the topology
make topology                  # health check
make switchover NEW_PRIMARY=db-02
make promote [NEW_PRIMARY=db-02] [FORCE=1]
make recover NODE=db-01
make destroy [DATA=1]
```

## Switching engine or strategy

Switching is never done in place:
1. `make destroy`. Use `make destroy DATA=1` when the server packages differ: another engine, or PXC.
2. Change `db_engine` and/or `ha_provider` in `config/ha.yml`.
3. `make provision`, then `make ha HA=<strategy>`.
