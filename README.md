# mysql-ha-lab

A lab for deploying, monitoring, benchmarking and comparing MySQL high-availability architectures across servers and data centers. It is not tied to any application.

- **Remote nodes** run MySQL, `mysqld_exporter` and `node_exporter`, all provisioned by Ansible.
- **Your machine** runs Ansible, Prometheus, Grafana and (later) JMeter.
- **Traffic** goes over Tailscale by default. Nodes can also use a private LAN or your own VPN, mixed per node. On unencrypted networks the lab enforces TLS. See [docs/architecture/networks.md](docs/architecture/networks.md).

```text
 LOCAL MACHINE (Tailscale)                         REMOTE NODES (Tailscale)
 ┌──────────────────────────────┐                 ┌───────────────────────┐
 │ Ansible ── provision ────────┼── SSH ─────────▶│ MySQL 8.4             │
 │ Prometheus ── scrape ────────┼── :19100/:19104▶│ node / mysqld exporter│  x N, any DC
 │ Grafana (dashboards)         │                 │ chrony, nftables      │
 │ JMeter ──────────────────────┼── :13306 ──────▶│ lab collector, probe  │
 └──────────────────────────────┘                 └───────────────────────┘
```

## Status

| Phase | Scope | State |
|---|---|---|
| 1–3 | Config schema and validation, Tailscale checks, provisioning, local Prometheus/Grafana | done |
| 4 | Async primary–replica: deploy, switchover, failover, recover, destroy | done; tested end to end on MySQL 8.4 containers |
| 5 | Unified metrics: node collector, network probes, client exporter, recording rules, 8 dashboards | done; collectors tested against MySQL 8.4 |
| 6 | JMeter: 8 scenarios + dataset preparation, nanosecond timing per transaction | done; tested against MySQL 8.4 |
| 7 | Semi-sync, Orchestrator, InnoDB Cluster (+ Router per node), Percona XtraDB Cluster | semi-sync tested in containers; Orchestrator, InnoDB Cluster and PXC need real systemd hosts and are **not yet tested** |
| 8 | Runner, run_id correlation, RTO/RPO, consistency validation, Markdown/JSON/CSV reports | done; tested with an injected outage |

## Requirements

**On your machine** (any OS; `make validate-local` checks the tools, not the OS)
- Ansible core 2.17 or later (tested with 2.21)
- Docker with Compose v2
- Tailscale (only when some nodes are on a `tailscale` network), plus a route to every node network
- `python3` (standard library only), `curl`, `openssl`, `nc`
- Java 8+ for JMeter (`make jmeter-install` downloads JMeter itself)

**Remote nodes**
- Ubuntu 22.04, Ubuntu 24.04 or Debian 12
- x86_64 only. repo.mysql.com publishes amd64 packages only. The exporters also support arm64.
- Reachable from this machine and from every other node: Tailscale with MagicDNS, a private network, or a VPN
- SSH access with a sudo-capable user
- Outbound internet access to repo.mysql.com, GitHub releases and the distribution mirrors

## Quick start

```bash
make deps                 # pinned Ansible collections into ansible/collections
make init                 # config/*.yml from the examples (gitignored)
$EDITOR config/inventory.yml config/ha.yml config/secrets.yml
ansible-vault encrypt config/secrets.yml
export ANSIBLE_VAULT_PASSWORD_FILE=~/.lab-vault-pass   # or: make <target> VAULT_ARGS=--ask-vault-pass

make validate             # local tools, config, secrets, addresses, SSH, sudo, node OS, full-mesh connectivity
make provision            # chrony, ufw, MySQL (or PXC), accounts, exporters, lab collector, network probe
make monitoring           # targets, Prometheus + Grafana, local client probe
make metrics-check        # promtool, direct exporter checks, Prometheus target health

make ha HA=async-replication   # build the topology chosen in config/ha.yml
make topology                  # health

make jmeter-install
make benchmark SCENARIO=mixed-workload
make failover CONFIRM=yes      # failover experiment: RTO, RPO, consistency
```

Open Grafana at http://127.0.0.1:3000. The user is `admin` and the password is in `local/compose/.env`. The **MySQL HA Lab** folder holds eight dashboards:
- HA Overview
- Latency Breakdown
- Network
- MySQL Performance
- Replication & HA
- Host & Storage
- JMeter Benchmark
- Failover & Recovery

## Configuration

| File | Purpose |
|---|---|
| `config/inventory.yml` | Environment name, SSH settings, networks (tailscale / vpn / private), nodes (name, address, network, role, datacenter, optional overrides) |
| `config/ha.yml` | HA provider, MySQL version and port, global my.cnf options, exporter ports |
| `config/monitoring.yml` | Scrape address mode, extra allowed sources (for example this machine's network), firewall behaviour, exporter versions |
| `config/secrets.yml` | exporter, replication, benchmark and admin passwords. Encrypt it with Ansible Vault. Keys are `db_<name>_password` (older `mysql_<name>_password` keys still work). Environment variables `LAB_DB_*_PASSWORD` (or `LAB_MYSQL_*_PASSWORD`) take precedence. |
| `config/benchmark.yml` | Client location, endpoint, dataset, load defaults, per-scenario overrides |

The `lab_config` inventory plugin (`ansible/plugins/inventory/lab_config.py`) turns these files into the Ansible inventory. It validates them before any play runs and lists every problem it finds. It checks:
- unique names, FQDNs, server IDs and Tailscale IPs
- exactly one `primary`
- that example FQDNs have been replaced
- node counts per provider (InnoDB Cluster: 3 to 9 nodes, others: at least 2 or 3)
- port collisions
- that SSH key files exist

Run `make validate-config` for an offline check.

Example node with overrides:

```yaml
nodes:
  - name: mysql-03
    fqdn: mysql-03.tail1234.ts.net
    role: replica
    datacenter: ap-southeast
    server_id: 103
    tailscale_ip: 100.101.102.103
    mysql:
      innodb_buffer_pool_size: 4G
```

## Commands

```text
Configuration   make init | deps | validate | validate-local | validate-config
Infrastructure  make plan-host | provision | configure-exporters | collect-info
Monitoring      make targets | monitoring | monitoring-stop | monitoring-status | metrics-check | probes-start | probes-stop
Network         make mesh-check | network-check | network-matrix [IPERF=1]
HA              make ha HA=<provider> | topology | switchover [NEW_PRIMARY=] | promote [NEW_PRIMARY=] [FORCE=1] | recover NODE=
Faults          make fault NODE= FAULT=stop-db|kill-db|isolate|netem [ACTION=restore] CONFIRM=yes
Benchmark       make jmeter-install | benchmark SCENARIO= [ENDPOINT=ha|primary|<node>] [J="k=v ..."] | failover CONFIRM=yes [FAULT=]
Reports         make report [RUN=<run_id>]
Status          make status
Teardown        make destroy [DATA=1]
Development     make lint | scenarios | dashboards
```

To pass extra Ansible arguments, use `ARGS`, for example `make provision ARGS="--limit mysql-02 --diff"`.

The scenarios are `baseline`, `read-heavy` (80/20), `write-heavy` (20/80), `mixed-workload` (50/50), `spike-test`, `soak-test`, `failover-test` and `network-disruption`. They all use the same schema and the same deterministic dataset. For the run workflow, the output files and the RTO/RPO definitions, see [docs/runbooks/benchmarking.md](docs/runbooks/benchmarking.md).

## Safety

- **Existing services are protected.** `make plan-host` is read-only. For each node it shows the packages, services, users, paths and ports the lab would add, and `make provision` runs it first. Provisioning stops before changing anything if:
  - a lab port is already in use (it names the process);
  - MariaDB or another MySQL server is installed;
  - the InnoDB buffer pool would not fit in available memory (set `mysql.buffer_pool_ratio`, e.g. 0.25, on shared servers).

  An existing ntp, ntpsec or openntpd is kept, and chrony is installed only when none of them is present. The repo.mysql.com pin applies to `mysql-*` packages only.
- **Existing data is protected.** Provisioning refuses to touch a MySQL datadir that the lab did not create. The lab marks its own datadirs with `/etc/mysql/.ha-lab-managed`. To take over an existing server anyway, pass `ARGS="-e mysql_adopt_existing=true"`. This changes its config and restarts it. It never deletes data.
- **Topology switches are explicit.** `make ha HA=x` refuses to run when `x` differs from `ha_provider` in `config/ha.yml`. Nodes record their deployed provider in `/etc/mysql/.ha-lab-provider`, and deployment refuses a different provider until you run `make destroy`.
- **Replication refuses errant GTIDs.** A replica that holds transactions the primary lacks is never attached. A switchover fences the old primary before it promotes the new one, and unfences it automatically if the promotion fails.
- **Faults need opt-in.** Every fault requires `CONFIRM=yes`. Isolation and netem also schedule a restore on the node itself.
- **Destructive targets need confirmation.** `make destroy` asks you to type the environment name. `DATA=1` also purges the server packages and the datadir.
- **Firewall.** By default the lab loads its own nftables table, `inet halab`, through `halab-firewall.service`. It covers only the lab ports (MySQL, HA, exporters). On each node's lab interface it accepts the node networks' `cidrs` plus `allowed_sources`, and drops everything else for those ports. Other ports and existing rules are untouched, and the host ruleset is never flushed. Remove it with `systemctl disable --now halab-firewall`. `firewall.backend: ufw` switches to host-wide ufw; `none` leaves the firewall to you.
- **Connectivity is verified before changes.** `make mesh-check` runs inside `validate`, `provision` and `ha`. It tests every node pair on the provider's ports, logs in to MySQL over TLS, and checks this machine's access to the client and monitoring ports.
- **Secrets stay out of Git.** The real config files, the secrets file and the generated targets are all gitignored.
- **Root access.** MySQL `root@localhost` keeps `auth_socket`. Ansible connects over the Unix socket as OS root, so the lab never manages a root password.

## Layout

```text
config/            user configuration (*.example.yml committed, real files gitignored)
ansible/           inventory plugin, engine-neutral playbooks and core roles (common, node-exporter, lab-collector)
ansible/engines/   one folder per database engine: descriptor, roles and hooks (mysql, postgres ready; mariadb planned)
ha/<provider>/     architecture, runbooks and limitations per HA provider
local/             Prometheus, Grafana (dashboards + generator), compose, helper scripts
metrics/           collectors (stdlib Python), metric schema, quantile recording rules
jmeter/            scenarios (generated), Groovy sampler, dataset preparation, installer
benchmark/         runner, JTL exporter, correlation/analysis, consistency validation, report generator, results/
docs/              architecture, runbooks, metrics
```

See [docs/architecture/foundation.md](docs/architecture/foundation.md) for design decisions, assumptions and known limitations, and [docs/architecture/engines.md](docs/architecture/engines.md) for the engine layout and how to add an engine.
