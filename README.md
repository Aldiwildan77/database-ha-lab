# mysql-ha-lab

A lab for deploying, monitoring, benchmarking and comparing MySQL high-availability architectures across servers and data centers. It is not tied to any application.

- **Remote nodes** run MySQL, `mysqld_exporter` and `node_exporter`, all provisioned by Ansible.
- **Your machine** runs Ansible, Prometheus, Grafana and (later) JMeter.
- **All traffic** goes over Tailscale. Nothing is exposed to the public internet.

```text
 LOCAL MACHINE (Tailscale)                         REMOTE NODES (Tailscale)
 ┌──────────────────────────────┐                 ┌───────────────────────┐
 │ Ansible ── provision ────────┼── SSH ─────────▶│ MySQL 8.4             │
 │ Prometheus ── scrape ────────┼── :9100/:9104 ─▶│ node / mysqld exporter│  x N, any DC
 │ Grafana (dashboards)         │                 │ chrony, ufw           │
 │ JMeter ──────────────────────┼── :3306 ───────▶│ lab collector, probe  │
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

**On your machine**
- Ansible core 2.17 or later (tested with 2.21)
- Docker with Compose v2
- Tailscale, connected to the same tailnet as the nodes
- `python3` (standard library only), `curl`, `openssl`, `nc`
- Java 8+ for JMeter (`make jmeter-install` downloads JMeter itself)

**Remote nodes**
- Ubuntu 22.04, Ubuntu 24.04 or Debian 12
- x86_64 only. repo.mysql.com publishes amd64 packages only. The exporters also support arm64.
- Joined to the tailnet with MagicDNS enabled
- SSH access with a sudo-capable user
- Outbound internet access to repo.mysql.com, GitHub releases and the distribution mirrors

## Quick start

```bash
make deps                 # pinned Ansible collections into ansible/collections
make init                 # config/*.yml from the examples (gitignored)
$EDITOR config/inventory.yml config/ha.yml config/secrets.yml
ansible-vault encrypt config/secrets.yml
export ANSIBLE_VAULT_PASSWORD_FILE=~/.lab-vault-pass   # or: make <target> VAULT_ARGS=--ask-vault-pass

make validate             # config, secrets, local Tailscale, DNS, tailscale ping, SSH, sudo, OS
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
| `config/inventory.yml` | Environment name, SSH settings, nodes (name, MagicDNS FQDN, role, datacenter, optional overrides) |
| `config/ha.yml` | HA provider, MySQL version and port, global my.cnf options, exporter ports |
| `config/monitoring.yml` | Scrape address mode, allowed source CIDRs, firewall behaviour, exporter versions |
| `config/secrets.yml` | exporter, replication, benchmark and admin passwords. Encrypt it with Ansible Vault. Environment variables `LAB_MYSQL_*_PASSWORD` take precedence. |
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
Configuration   make init | deps | validate | validate-config
Infrastructure  make provision | configure-exporters | collect-info
Monitoring      make targets | monitoring | monitoring-stop | monitoring-status | metrics-check | probes-start | probes-stop
Network         make network-check | network-matrix [IPERF=1]
HA              make ha HA=<provider> | topology | switchover [NEW_PRIMARY=] | promote [NEW_PRIMARY=] [FORCE=1] | recover NODE=
Faults          make fault NODE= FAULT=stop-mysql|kill-mysql|isolate|netem [ACTION=restore] CONFIRM=yes
Benchmark       make jmeter-install | benchmark SCENARIO= [ENDPOINT=ha|primary|<node>] [J="k=v ..."] | failover CONFIRM=yes [FAULT=]
Reports         make report [RUN=<run_id>]
Status          make status
Teardown        make destroy [DATA=1]
Development     make lint | scenarios | dashboards
```

To pass extra Ansible arguments, use `ARGS`, for example `make provision ARGS="--limit mysql-02 --diff"`.

The scenarios are `baseline`, `read-heavy` (80/20), `write-heavy` (20/80), `mixed-workload` (50/50), `spike-test`, `soak-test`, `failover-test` and `network-disruption`. They all use the same schema and the same deterministic dataset. For the run workflow, the output files and the RTO/RPO definitions, see [docs/runbooks/benchmarking.md](docs/runbooks/benchmarking.md).

## Safety

- **Existing data is protected.** Provisioning refuses to touch a MySQL datadir that the lab did not create. The lab marks its own datadirs with `/etc/mysql/.ha-lab-managed`. To take over an existing server anyway, pass `ARGS="-e mysql_adopt_existing=true"`. This changes its config and restarts it. It never deletes data.
- **Topology switches are explicit.** `make ha HA=x` refuses to run when `x` differs from `ha_provider` in `config/ha.yml`. Nodes record their deployed provider in `/etc/mysql/.ha-lab-provider`, and deployment refuses a different provider until you run `make destroy`.
- **Replication refuses errant GTIDs.** A replica that holds transactions the primary lacks is never attached. A switchover fences the old primary before it promotes the new one, and unfences it automatically if the promotion fails.
- **Faults need opt-in.** Every fault requires `CONFIRM=yes`. Isolation and netem also schedule a restore on the node itself.
- **Destructive targets need confirmation.** `make destroy` asks you to type the environment name. `DATA=1` also purges the server packages and the datadir.
- **Firewall.** ufw denies incoming traffic by default. SSH and Tailscale UDP 41641 are allowed first, so you are not locked out. MySQL (3306) and the exporters (9100, 9104) are allowed only on `tailscale0` and only from `allowed_sources`, which defaults to the whole tailnet. Narrow it to this machine's Tailscale IP, and use Tailscale ACLs as the primary control.
- **Secrets stay out of Git.** The real config files, the secrets file and the generated targets are all gitignored.
- **Root access.** MySQL `root@localhost` keeps `auth_socket`. Ansible connects over the Unix socket as OS root, so the lab never manages a root password.

## Layout

```text
config/            user configuration (*.example.yml committed, real files gitignored)
ansible/           inventory plugin, playbooks, roles (common, mysql, node-exporter, mysql-exporter,
                   lab-collector, replication, orchestrator, innodb-cluster, percona-xtradb-cluster, percona-repo)
ha/<provider>/     architecture, runbooks and limitations per HA provider
local/             Prometheus, Grafana (dashboards + generator), compose, helper scripts
metrics/           collectors (stdlib Python), metric schema, quantile recording rules
jmeter/            scenarios (generated), Groovy sampler, dataset preparation, installer
benchmark/         runner, JTL exporter, correlation/analysis, consistency validation, report generator, results/
docs/              architecture, runbooks, metrics
```

See [docs/architecture/foundation.md](docs/architecture/foundation.md) for design decisions, assumptions and known limitations.
