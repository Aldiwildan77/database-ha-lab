# MySQL InnoDB Cluster (Group Replication, single-primary)

## Architecture

```text
 client ──▶ router@mysql-01:6446 ┐
        ──▶ router@mysql-02:6446 ├─▶ PRIMARY (writable) ◀─ group (Paxos) ─▶ SECONDARY, SECONDARY
        ──▶ router@mysql-03:6446 ┘
```

- MySQL 8.4 LTS, Group Replication in single-primary mode, managed with MySQL Shell's AdminAPI.
- The communication stack is `MYSQL`: group traffic uses the MySQL port (3306), so there is no separate XCom port.
- 3 to 9 members. 3 is the baseline, which tolerates 1 failure.
- A commit returns only after the group has **certified** the transaction and a majority has received it. Members apply it asynchronously after that.

## Router and endpoints

- MySQL Router is installed and bootstrapped on **every node**. RW is on `rw_port` (6446) and RO on `rw_port+1` (6447).
- The lab's `ha` endpoint lists every router and uses the first one that answers. Losing one node therefore loses one router, not the endpoint. **Router is not a single point of failure.**
- A production deployment would instead run Router next to each application host, or behind a load balancer or DNS name that spans several routers. Either choice is a client-side decision.
- Each router follows cluster metadata. After a failover, RW connections go to the new primary within seconds.

## Deploy

```bash
# config/ha.yml: ha_provider: innodb-cluster; secrets: mysql_admin_password
make ha HA=innodb-cluster
```

The playbook runs these steps:
1. Installs `mysql-shell` and `mysql-router`.
2. Creates the `clusteradmin` account on every node, with `sql_log_bin=0`.
3. Runs `dba.configureInstance()` and restarts the node if needed.
4. Creates the cluster on the primary node from the inventory (the seed), if it does not exist.
5. Adds or rejoins the other members **one at a time** with `recoveryMethod: clone`. **Clone replaces the joiner's data with the donor's.**
6. Bootstraps the routers.

Re-running it is idempotent.

## Failover and recovery

- **Automatic.** When the primary fails, the remaining majority elects a new primary, and the routers redirect within a few seconds. Expect roughly 5–10 s with default timeouts, plus the client reconnect.
- **Planned switchover:** `make switchover NEW_PRIMARY=mysql-02`. This runs `cluster.setPrimaryInstance()`.
- **Recovery:** `make recover NODE=mysql-01`. This starts MySQL and calls `cluster.rejoinInstance()` if the node does not come back ONLINE by itself.
- **Loss of majority** (for example 2 of 3 members down): the group blocks writes. Restore with `dba.rebootClusterFromCompleteOutage()` or `cluster.forceQuorumUsingPartitionOf()`. These are manual and deliberate, and not automated here.

## Consistency

The default is `group_replication_consistency=EVENTUAL`. A read on a secondary right after a write may not see it yet. The benchmark sends all traffic to the RW port. Write conflicts are detected by certification, so a transaction can fail at COMMIT. The sampler classifies that as `conflict`.

## Monitoring

The *Replication & HA* dashboard, Group Replication row:

| Metric | What it shows |
|---|---|
| `ha_lab_group_members{state}` | Member count by state |
| `ha_lab_group_primary` | Which member is the primary |
| `replication_queue_size{queue="certification\|applier"}` | Certification and applier backlog |
| `replication_certification_conflicts_total` | Certification conflicts |
| `mysql_perf_schema_*` (mysqld_exporter) | Group Replication details |

## Known limitations

- Flow-control throttling time is not exposed. Watch the queues.
- Router does not expose Prometheus metrics.
- Every transaction needs a majority round trip. Across data centers, commit latency follows the RTT to the second-closest member.
