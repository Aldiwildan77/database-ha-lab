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
 │ JMeter (Phase 6) ────────────┼── :3306 ───────▶│                       │
 └──────────────────────────────┘                 └───────────────────────┘
```

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Repo layout, config schema and validation, Tailscale checks | done |
| 2 | Common, MySQL and exporter roles | done |
| 3 | Local Prometheus/Grafana, generated targets, MySQL + Host dashboards | done |
| 4 | Async primary-replica, failover runbook | next |
| 5 | Unified metrics, network collectors, latency breakdown | planned |
| 6 | JMeter scenarios | planned |
| 7 | Orchestrator, semi-sync, InnoDB Cluster, Percona XtraDB Cluster | planned |
| 8 | Reports, RTO/RPO, consistency validation | planned |

Make targets for later phases (`ha`, `benchmark`, `failover`, `report`, `network-matrix`, `destroy`) validate their arguments and then exit with code 2 and a "not implemented" message. They change nothing.

## Requirements

**On your machine**
- Ansible core 2.17 or later (tested with 2.21)
- Docker with Compose v2
- Tailscale, connected to the same tailnet as the nodes
- `python3`, `curl`, `openssl`, `nc`

**Remote nodes**
- Ubuntu 22.04, Ubuntu 24.04 or Debian 12
- x86_64 only. repo.mysql.com publishes amd64 packages only. The exporters also support arm64.
- Joined to the tailnet with MagicDNS enabled
- SSH access with a sudo-capable user
- Outbound internet access to repo.mysql.com, GitHub releases and the distribution mirrors

## Quick start

```bash
make deps                 # install pinned Ansible collections into ansible/collections
make init                 # create config/*.yml from the examples (gitignored)
$EDITOR config/inventory.yml config/ha.yml config/secrets.yml
ansible-vault encrypt config/secrets.yml
export ANSIBLE_VAULT_PASSWORD_FILE=~/.lab-vault-pass   # or: make <target> VAULT_ARGS=--ask-vault-pass

make validate             # config, secrets, local Tailscale, DNS, tailscale ping, SSH, sudo, OS
make provision            # chrony, ufw, MySQL, exporters; checks exporters from this machine
make monitoring           # generate targets, start Prometheus + Grafana
make metrics-check        # promtool, direct exporter checks, Prometheus target health
```

Open Grafana at http://127.0.0.1:3000. The user is `admin`. The password was generated into `local/compose/.env`. Open the **MySQL HA Lab** folder to find the **MySQL Overview** and **Host & Storage Overview** dashboards.

## Configuration

| File | Purpose |
|---|---|
| `config/inventory.yml` | Environment name, SSH settings, nodes (name, MagicDNS FQDN, role, datacenter, optional overrides) |
| `config/ha.yml` | HA provider, MySQL version and port, global my.cnf options, exporter ports |
| `config/monitoring.yml` | Scrape address mode, allowed source CIDRs, firewall behaviour, exporter versions |
| `config/secrets.yml` | Passwords. Encrypt it with Ansible Vault. Environment variables `LAB_MYSQL_*_PASSWORD` take precedence. |
| `config/benchmark.yml` | JMeter settings, used from Phase 6 |

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
make init | deps | validate | validate-config
make provision | configure-exporters | collect-info
make targets | monitoring | monitoring-stop | monitoring-status | metrics-check
make network-check            # DNS, tailscale ping (DERP vs direct path + latency), TCP ports
make status
make lint                     # ansible-lint (production profile), shellcheck, compose config
```

Pass extra Ansible arguments with `ARGS`, for example `make provision ARGS="--limit mysql-02 --diff"`.

## Safety

- **Existing data is protected.** Provisioning refuses to touch a MySQL datadir that the lab did not create. The lab marks its own datadirs with `/etc/mysql/.ha-lab-managed`. To take over an existing server anyway, pass `ARGS="-e mysql_adopt_existing=true"`. This changes its config and restarts it. It never deletes data.
- **Topology switches are explicit.** `make ha HA=x` refuses to run when `x` differs from `ha_provider` in `config/ha.yml`.
- **Destructive targets need confirmation.** `make destroy` asks you to type the environment name.
- **Firewall.** ufw denies incoming traffic by default. SSH and Tailscale UDP 41641 are allowed first, so you are not locked out. MySQL (3306) and the exporters (9100, 9104) are allowed only on `tailscale0` and only from `allowed_sources`, which defaults to the whole tailnet. Narrow it to this machine's Tailscale IP, and use Tailscale ACLs as the primary control.
- **Secrets stay out of Git.** The real config files, the secrets file and the generated targets are all gitignored.
- **Root access.** MySQL `root@localhost` keeps `auth_socket`. Ansible connects over the Unix socket as OS root, so the lab never manages a root password.

## Layout

```text
config/            user configuration (*.example.yml committed, real files gitignored)
ansible/           ansible.cfg points here: inventory plugin, playbooks, roles, shared tasks
  roles/           common, mysql, node-exporter, mysql-exporter
local/             Prometheus, Grafana, compose, helper scripts (run on your machine)
ha/                per-provider modules (Phase 4+)
jmeter/            scenarios, data, results (Phase 6)
metrics/           metric schema, collectors, recording rules (Phase 5)
benchmark/         runner, correlation, validation, report (Phase 8)
docs/              architecture notes, runbooks
```

See [docs/architecture/foundation.md](docs/architecture/foundation.md) for design decisions, assumptions and known limitations.
