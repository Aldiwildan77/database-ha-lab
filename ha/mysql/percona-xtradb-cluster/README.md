# Percona XtraDB Cluster (Galera)

## Architecture

```text
 node A ◀──── wsrep (gcomm, 14567) ────▶ node B
    ▲                                     ▲
    └──────────────▶ node C ◀─────────────┘     every node is writable (multi-primary)
```

- Percona XtraDB Cluster 8.4 LTS (`pxc-84-lts`) or 8.0 (`pxc-80`), with Galera 4.
- These packages **replace** Oracle MySQL. The `ha_provider` must be chosen before provisioning.
- At least 3 nodes, an odd number recommended. Quorum is a majority of the last primary component.
- `pxc_strict_mode=ENFORCING`, `wsrep_sst_method=xtrabackup-v2`.
- `pxc_encrypt_cluster_traffic` is OFF on encrypted networks (Tailscale or VPN) and is **forced ON** when any node is on a `private` network. When it is on, the bootstrap node's CA and certificates are copied to every node (`/etc/mysql/pxc-certs`) before the cluster forms.
- Ports open on each node's lab interface:
  - 13306 (MySQL);
  - 14567 (group communication, `gcomm_port`);
  - 14568 (IST, `ist_port`);
  - 14444 (SST, `sst_port`).

## Deploy

For PXC, the cluster is formed during provisioning, because the server packages themselves differ:

```bash
# config/ha.yml: ha_provider: percona-xtradb-cluster
make provision      # installs PXC, bootstraps the primary node, joins the others one by one
make ha HA=percona-xtradb-cluster   # verifies the cluster and records the provider
```

Provisioning refuses nodes that already run Oracle MySQL, or that have a datadir the lab did not create. Use `make destroy DATA=1` first (this deletes data).

The first formation runs these steps:
1. `mysql@bootstrap` starts on the primary node.
2. The other nodes start one at a time. Each receives a full SST, which replaces the joiner's datadir.
3. The bootstrap node switches to the normal service and rejoins through IST.

## Writes, conflicts and latency

- Every commit is certified on all nodes **synchronously**. Commit latency includes a round trip to every member, so the slowest link sets the pace.
- Writes on different nodes to the same rows can conflict. The loser fails at COMMIT with a deadlock error (1213), which the sampler classifies as `deadlock`. Plan for this before sending writes to several nodes.
- **Flow control** (`wsrep_flow_control_paused`) throttles the whole cluster when any node's receive queue grows.

## Failover and recovery

- There is no primary to promote. Any node in the primary component accepts writes.
- With the `ha` endpoint, clients connect to the first reachable node and reconnect elsewhere on failure.
- **Losing a minority:** the rest continue.
- **Losing a majority, or a network split:** the remaining nodes go `non-Primary` and refuse queries (`wsrep_ready=OFF`). This prevents split brain.
- **Node returns:** `make recover NODE=<n>` starts MySQL. The node catches up through IST if the gcache still holds the missing writesets, otherwise through a full SST.
- **Full-cluster outage:** find the node with the highest `seqno` in `grastate.dat`, set `safe_to_bootstrap: 1`, start it with `mysql@bootstrap`, then start the others. This is manual and deliberate.

## Monitoring

The *Replication & HA* dashboard, Galera row:

| Metric | What it shows |
|---|---|
| `ha_lab_wsrep_cluster_size` | Cluster size |
| `ha_lab_wsrep_primary_component` | Whether the node is in the primary component (quorum) |
| `replication_queue_size{queue="wsrep_recv\|wsrep_send"}` | Receive and send queues |
| `mysql_global_status_wsrep_flow_control_paused` | Flow-control pause fraction |
| `mysql_global_status_wsrep_local_cert_failures` | Certification failures |

## Known limitations

- There is no per-transaction replication apply timing (wsrep has no Performance Schema applier timestamps).
- The `role` label (primary/replica) only marks the bootstrap node. Every node is writable.
- Changing the PXC configuration needs a rolling restart, one node at a time: restart, then `make recover`.
