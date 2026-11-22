# MariaDB Galera Cluster

## Architecture

```text
 node A ◀──── wsrep (gcomm, 14567) ────▶ node B
    ▲                                     ▲
    └──────────────▶ node C ◀─────────────┘     every node is writable (multi-primary)
```

- MariaDB 11.4 LTS (10.11 or 11.8) from `deb.mariadb.org`, with `galera-4` (`/usr/lib/galera/libgalera_smm.so`). The server packages are the same as for the replication strategies.
- At least 3 nodes, an odd number recommended. Quorum is a majority of the last primary component.
- `wsrep_sst_method=mariabackup` (the donor stays writable during the copy), `binlog_format=ROW`, `innodb_autoinc_lock_mode=2`, `wsrep_gtid_mode=ON`.
- The lab writes `/etc/mysql/mariadb.conf.d/zz-ha-lab-galera.cnf`. `wsrep_node_address` and the IST/SST receive addresses are the node's inventory `address`, so every node must reach every other node at that address.
- Cluster traffic is encrypted when any node is on a `private` network (forced) or when `mariadb_galera.encrypt_cluster_traffic: true`. The role generates one self-signed certificate on the bootstrap node, copies it to every node (`/etc/mysql/halab-galera-tls`) and uses it for:
  - group communication and IST: `socket.ssl_cert`, `socket.ssl_key`, `socket.ssl_ca`;
  - SST: `[sst] encrypt=4`, `ssl-mode=VERIFY_CA` (socat over TLS, both ends present the certificate).
- Ports open on each node's lab interface (all differ from the product standard):
  - 13306 (MariaDB, standard 3306);
  - 14567 (group communication, `gcomm_port`, standard 4567);
  - 14568 (IST, `ist_port`, standard 4568);
  - 14444 (SST, `sst_port`, standard 4444).

Settings in `config/ha.yml` → `mariadb_galera`:

| Setting | Default | Meaning |
|---|---|---|
| `cluster_name` | `halab-galera` | `wsrep_cluster_name` |
| `sst_method` | `mariabackup` | `mariabackup` or `rsync`. `rsync` blocks the donor and is not encrypted, so it is refused when traffic must be encrypted. |
| `gcomm_port`, `ist_port`, `sst_port` | 14567, 14568, 14444 | Lab ports |
| `gtid_domain_id` | 1 | `wsrep_gtid_domain_id`, the same on every node |
| `encrypt_cluster_traffic` | off on Tailscale/VPN, forced on for `private` | TLS for gcomm, IST and SST |

## Deploy

```bash
# config/ha.yml: db_engine: mariadb, ha_provider: galera
make provision      # standalone MariaDB on every node (same as async-replication)
make ha HA=galera   # forms the cluster, then checks it
```

`make ha` runs these steps:
1. Installs `galera-4`, `mariadb-backup` and `socat`, and writes the Galera configuration.
2. When no node holds a primary component: stops the standalone servers, starts the node with role `primary` with `galera_new_cluster`, then starts the others one at a time. Each joiner receives a full SST copy of the bootstrap node, which replaces its own datadir.
3. When a primary component exists: starts the nodes that are outside it, and restarts members whose configuration or certificate changed, one at a time.
4. Waits until every node is `Synced` in a primary component of the full size.

Running it again changes nothing.

Protections:
- Provisioning refuses a datadir the lab did not create (`-e mariadb_adopt_existing=true` overrides), as for the other MariaDB strategies.
- A joiner with tables outside the system schemas (for example benchmark data from a former replication topology) is refused, because the SST would replace them. Pass `-e mariadb_galera_rebuild_joiner=true` to accept that.
- When every node is down but `grastate.dat` shows that the cluster ran before, `make ha` refuses to bootstrap the role-primary node, which may not be the most advanced. Use `make recover` (below).

## Writes, durability and conflicts

- **Synchronous replication, asynchronous apply.** A COMMIT returns after the writeset is replicated to every node and certified. Other nodes apply it shortly afterwards; set `wsrep_sync_wait` in a session that must read its own writes on another node.
- **No data loss while a primary component exists.** A committed transaction is in every member's queue. Losing a minority loses nothing.
- **Certification conflicts.** Writes on different nodes to the same rows race. The loser fails at COMMIT with a deadlock error (1213, `wsrep_local_cert_failures` grows), which the benchmark classifies as `deadlock`. Send conflicting writes to one node, or retry.
- **Flow control.** When any node's receive queue exceeds `gcs.fc_limit`, the whole cluster pauses writes (`wsrep_flow_control_paused`). The slowest node sets the pace.
- **Commit latency** includes a round trip to the farthest node. Across data centers that dominates.
- **Quorum loss.** When a network split or a crash leaves no majority, the remaining nodes go `non-Primary`: they reject reads and writes (`WSREP has not yet prepared node for application use`, `wsrep_ready=OFF`). This prevents split brain; the cluster is unavailable until a primary component is re-formed. Nodes stopped cleanly leave the cluster and shrink the quorum base, so stopping 2 of 3 nodes one at a time keeps the third writable; crashing them does not.

## Health

`make topology` shows `wsrep_local_state_comment` per node and passes only when every node is up, `Synced`, in the `Primary` component, and reports the same cluster size.

When it fails it says which case applies:
- `Degraded MariaDB Galera cluster: quorum held, 2 of 3 nodes ...`: writes continue on the remaining nodes; recover the missing one.
- `MariaDB Galera cluster without quorum`: no node accepts writes; run `make recover`.

A node outside the primary component is reported `read_only=True`, so it is never counted as writable.

## Failover and switchover

There is no primary to promote. Every node in the primary component accepts writes, and the `ha` endpoint connects to the first writable node and reconnects elsewhere on failure. `make switchover` and `make promote` only report this.

## Recovery runbooks

All cases use the same command: `make recover NODE=<node>`. It reads every node's state and picks the matching case.

**One node stopped or crashed, quorum held.** The node is started; it rejoins through IST when the donor's gcache (128 MB by default) still holds the missing writesets, otherwise through a full SST.

**Quorum lost, some nodes still running (`non-Primary`).** The running node with the highest `wsrep_last_committed` is declared primary (`pc.bootstrap`; on 11.4 `SET GLOBAL wsrep_provider_pc_bootstrap=ON`), then every stopped node is started and rejoins it. Only do this once the failed side is really down or isolated: two sides bootstrapped independently diverge.

**Full-cluster outage (every node down).**
1. `grastate.dat` is read on every node. Where `seqno` is -1 (unclean stop), `galera_recovery` reads the committed position from InnoDB.
2. All positions must be readable and belong to one cluster UUID. Otherwise the playbook refuses, because an unreadable node may hold newer commits.
3. The node with the highest seqno is chosen (ties: `safe_to_bootstrap: 1`, then the role-primary node). Its `grastate.dat` gets `safe_to_bootstrap: 1` and it starts with `galera_new_cluster`.
4. The other nodes start one at a time and rejoin through IST or SST.

When a node cannot be read and you accept losing its unreplicated commits, choose the bootstrap node yourself:

```bash
make recover NODE=db-01 ARGS="-e mariadb_galera_bootstrap_node=db-02"
```

**Manual equivalent** (on the chosen node): `cat /var/lib/mysql/grastate.dat`, `galera_recovery` where `seqno: -1`, set `safe_to_bootstrap: 1` on the highest, `galera_new_cluster`, then `systemctl start mariadb` on the others.

**Configuration change.** `make ha` restarts changed members one at a time. A `make provision` that changes `zz-ha-lab.cnf` restarts every node at once through the `mariadb` role's handler, which can take the cluster down; run `make recover` afterwards, or change settings one node at a time.

## Destroy

- `make destroy`: dissolves the cluster in place. Each node stops, the Galera configuration, certificate and state files are removed, and the node restarts as a standalone server with a full copy of the data. The copies diverge from then on; to build another topology from a clean state, use `DATA=1`.
- `make destroy DATA=1`: purges the MariaDB and Galera packages, the datadir and `/etc/mysql`. `socat` stays installed.

## Monitoring

| Metric | What it shows |
|---|---|
| `ha_lab_wsrep_cluster_size` | Cluster size |
| `ha_lab_wsrep_primary_component` | Whether the node is in the primary component (quorum) |
| `ha_lab_wsrep_ready` | Whether the node accepts queries |
| `ha_lab_writable` | `read_only` off, wsrep ready and primary |
| `replication_queue_size{queue="wsrep_recv\|wsrep_send"}` | Receive and send queues |
| `ha_lab_replication_position` | Sum of `gtid_binlog_pos` sequence numbers; equal on converged nodes |
| `mysql_global_status_wsrep_flow_control_paused` | Flow-control pause fraction (mysqld_exporter) |
| `mysql_global_status_wsrep_local_cert_failures` | Certification failures (mysqld_exporter) |

## Verified

In three Ubuntu 24.04 systemd containers (MariaDB 11.4.14, arm64, private network, so with TLS): provisioning, `make ha` (bootstrap and SST over TLS), a second `make ha` with nothing changed, writes on one node read on another, one node stopped (the other two stay `Primary` and writable, topology reports degraded) and recovered through IST, two nodes crashed (quorum loss) and recovered, a full clean stop and a full crash recovered from the highest seqno with no lost rows, the joiner-data refusal and its override, `make ha` refused while the whole cluster is down, destroy without data (standalone nodes) and with data (nothing left). Also run: the collector module against a Galera node.

Not run: real hosts, the nftables/ufw firewall, the exporters, `rsync` SST, Tailscale/VPN networks (no TLS), JMeter benchmarks and the `consistency.yml` ledger check, faults (`isolate`, `netem`), MariaDB 10.11 and 11.8.

## Known limitations

- The `role` label (primary/replica) only marks the bootstrap node. Every node is writable.
- There is no per-transaction apply timing (wsrep has no applier timestamps in the lab's metrics).
- One certificate is shared by all nodes and acts as its own CA; peers are verified against it, not by host name.
- The quorum-loss path trusts `wsrep_last_committed` of the running nodes; it does not read crashed nodes, which may have applied a little more.
- `recover.yml` acts on every node when the whole cluster is down, not only on `NODE`.
