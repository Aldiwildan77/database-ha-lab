# HA providers

One directory per provider: architecture notes, configuration templates, health checks, failover and recovery runbooks, limitations.

| Provider | Phase | Durability model |
|---|---|---|
| async-replication | 4 | Asynchronous; acknowledged commits can be lost on primary failure |
| orchestrator | 7 | Async + automated topology recovery; same loss window as async |
| semi-sync | 7 | Waits for replica receipt (not apply) up to a timeout, then degrades to async |
| innodb-cluster | 7 | Group Replication certification, majority agreement before commit |
| percona-xtradb-cluster | 7 | Galera certification, quorum-based, multi-primary capable |

These guarantees are not equivalent; benchmark reports state the measured RPO, never an assumed one.
