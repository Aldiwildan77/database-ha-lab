# Networks

Each node belongs to one network defined in `config/inventory.yml`. Nodes may span several networks, for example some on Tailscale and some on a data-center LAN, as long as routing connects them.

```yaml
environment:
  name: lab
  network: tailnet                     # default for nodes without `network:`
networks:
  tailnet:  {type: tailscale}                                     # tailscale0, 100.64.0.0/10 + fd7a:115c:a1e0::/48
  dc-lan:   {type: private, interface: eth1, cidrs: [10.20.0.0/16]}
  site-vpn: {type: vpn,     interface: wg0,  cidrs: [10.99.0.0/24]}
nodes:
  - {name: mysql-01, address: mysql-01.tail1234.ts.net, network: tailnet, role: primary, datacenter: dc-01}
  - {name: mysql-02, address: 10.20.0.12,               network: dc-lan,  role: replica, datacenter: dc-02}
```

If `networks` is omitted, every node is on one Tailscale network. That is the original behaviour, so older configs keep working.

## What each type changes

| | tailscale | vpn | private |
|---|---|---|---|
| Interface | `tailscale0` | yours (`wg0`, `tun0`, ...) | yours (`eth1`, `bond0`, ...) |
| Address check | resolves to a Tailscale IP; `tailscale ping` | resolves inside `cidrs`; interface IP inside `cidrs` | same as vpn |
| Tailscale required on the node and this machine | yes | no | no |
| Treated as encrypted | yes | yes | **no** |

On every network type:
- **Firewall (nftables `inet halab` by default):** lab ports are accepted on the node's interface from every used network's `cidrs` plus `monitoring.yml` `allowed_sources`, and dropped from anywhere else. Other ports and rules are untouched.
- **MySQL account hosts:** the lab creates one account per IPv4 CIDR above, as a `net/mask` host pattern.
- **Faults:** `isolate` (its own nftables table, `inet halab_fault`) and `netem` act on the node's interface.

### Private networks get TLS enforced

If any node is on a network with `encrypted: false`:

| Traffic | Enforcement |
|---|---|
| Server (TCP clients) | `require_secure_transport=ON` |
| Replication channel | TLS (`replication.ssl` cannot be false) |
| JMeter (JDBC) | `sslMode=REQUIRED` |
| InnoDB Cluster | `memberSslMode: REQUIRED` |
| Percona XtraDB Cluster | `pxc_encrypt_cluster_traffic=ON`, with the bootstrap node's CA and certificates copied to every node |

These remain unencrypted on a private network:
- exporter HTTP, i.e. the metrics themselves;
- the Orchestrator HTTP API (basic auth) and its raft traffic.

Restrict `allowed_sources`, or put those networks behind a VPN, if that matters to you.

## Mixed networks

The lab does not create routes between networks. You provide them, for example:
- a Tailscale subnet router;
- a site-to-site VPN;
- the routing between your data-center VLANs.

Two warnings:
- **SNAT changes the source address.** A subnet router or NAT may make nodes see a different source IP than the client's own. If so, add that source range to `allowed_sources` so the firewall and the MySQL host patterns accept it.
- **Paths are not comparable.** Benchmarks across mixed paths combine different latency profiles. Reports show each node's network, and the network matrix shows the path for every pair.

## Connectivity checks

`make mesh-check` also runs inside `make validate`, at the end of `make provision`, and before `make ha`. It checks:

1. **Node ↔ node, every ordered pair:** TCP on every port the provider needs.

   | Provider | Ports |
   |---|---|
   | All | MySQL |
   | Orchestrator | + 13000, 13008 |
   | PXC | + 14444, 14567, 14568 |

   If a port has no service yet, a temporary listener answers instead. This tests routing and firewall rules before the software is installed.
2. **Node → node, where MySQL already runs:** a real login over TLS with the replication account (or the benchmark account if no replication password is set). This proves DNS, routing, firewall, account host patterns and TLS together.
3. **This machine → every node:** the client ports (MySQL 13306, plus Router 16446/16447 for InnoDB Cluster) and the monitoring ports (19100, 19104, 19106).

These are the lab's default ports. They deliberately differ from each product's standard port, and all are configurable in `config/ha.yml`; see [foundation.md](foundation.md#ports).
