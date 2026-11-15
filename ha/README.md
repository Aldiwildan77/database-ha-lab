# HA providers

Each directory documents one architecture: its design, how to deploy it, health checks, failover and recovery runbooks, monitoring and known limitations. The Ansible roles live in `ansible/roles/{replication,orchestrator,innodb-cluster,percona-xtradb-cluster}`.

| Provider | Failover | Commit acknowledged when | Data loss on primary failure |
|---|---|---|---|
| [async-replication](async-replication/README.md) | manual (`make promote`) | the primary has written it locally | possible: anything not yet received by a replica |
| [semi-sync](semi-sync/README.md) | manual | ≥ N replicas received it (relay log), or the timeout expired | possible after a timeout fallback |
| [orchestrator](orchestrator/README.md) | automatic (raft-coordinated) | same as async | same as async; orchestrator moves topology, it adds no durability |
| [innodb-cluster](innodb-cluster/README.md) | automatic (group election) | a majority received it and certification passed | not for majority-acknowledged commits while quorum holds |
| [percona-xtradb-cluster](percona-xtradb-cluster/README.md) | none needed (multi-primary) | every node certified it | not while a primary component exists; a lost quorum blocks writes |

These guarantees differ. Benchmark reports state the **measured** RPO and RTO for each run, never an assumed one.

Switching providers is never done in place:
1. `make destroy` (or `make destroy DATA=1` when the server packages differ, as they do for PXC).
2. Change `ha_provider` in `config/ha.yml`.
3. `make provision`, then `make ha HA=<provider>`.
