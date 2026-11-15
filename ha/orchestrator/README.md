# Asynchronous replication + Orchestrator

## Architecture

```text
 orchestrator (raft) ── orchestrator (raft) ── orchestrator (raft)     one per MySQL node, SQLite backend
        │ polls every node every instance_poll_seconds, detects failure, promotes
 primary ──binlog──▶ replica ──▶ replica
```

- Replication itself is the [async-replication](../async-replication/README.md) setup.
- Orchestrator is a **topology manager, not a durability mechanism**. It decides who becomes primary. It cannot recover transactions that never left the failed primary.
- Orchestrator runs on every MySQL node: Percona's `percona-orchestrator` package, which supports MySQL 8.4, from the `pdps-84-lts` / `pdps-80` repository. Its own raft consensus means there is no external backend database and no single orchestrator instance.
- At least **3 nodes** are required, so raft has a quorum. An odd number is recommended.
- The HTTP API (lab default port 13000) uses basic auth. The user is `orchestrator`, and the password is `db_admin_password`.

## Deploy

```bash
# config/ha.yml: ha_provider: orchestrator; secrets: db_replication_password, db_admin_password
make ha HA=orchestrator
```

The playbook runs these steps:
1. Builds async replication.
2. Creates the `orchestrator` topology account on every node.
3. Installs and configures orchestrator.
4. Waits for a raft leader.
5. Discovers the topology from the primary.
6. Waits until every node is known.

## Automated failover

Orchestrator settings:
- `RecoverMasterClusterFilters: ["*"]`
- `ApplyMySQLPromotionAfterMasterFailover: true`: the promoted node gets `read_only=OFF` and `RESET REPLICA ALL`.
- `DelayMasterPromotionIfSQLThreadNotUpToDate: true`: it waits for the relay log to be applied.
- `RecoveryPeriodBlockSeconds`: anti-flapping. No second automatic recovery within this window.

Detection and promotion hooks append timestamped lines to `/var/log/orchestrator-failover.log` on the raft leader.

Inspect from any node:

```bash
ORCHESTRATOR_API="http://<node>:13000/api" ORCHESTRATOR_AUTH_USER=orchestrator ORCHESTRATOR_AUTH_PASSWORD=... \
  orchestrator-client -c topology -alias halab
orchestrator-client -c audit-recovery   # recovery history
```

## Split brain and its limits

- Raft prevents two orchestrator nodes from running a recovery at the same time.
- Orchestrator **does not fence** the old primary. If the old primary is only partitioned from orchestrator (not from clients), clients that still reach it keep writing to it. The lab does not install STONITH or a proxy. Clients must route by `read_only`. The benchmark's `ha` mode does this, as would ProxySQL.
- When the old primary returns, it is still writable and is not reattached. Use `make recover NODE=<old primary>`. It refuses if the node holds errant transactions, which are lost data.

## Planned switchover

```bash
make switchover [NEW_PRIMARY=mysql-02]   # graceful-master-takeover-auto
```

## Monitoring

- Use the same metrics as async replication.
- Detection and promotion timing comes from the benchmark poller (client clock) and from the orchestrator failover log.
- Orchestrator has no Prometheus endpoint. Its recovery audit is in the API.

## Known limitations

- Failover is still lossy for unreplicated transactions.
- There is no fencing, as described above.
- The orchestrator HTTP API is reachable from `allowed_sources`. Restrict that list, or use Tailscale ACLs.
