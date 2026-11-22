# database-ha-lab

A lab for deploying, monitoring, benchmarking and comparing database high-availability architectures across servers and data centers. It supports MySQL, MariaDB and PostgreSQL behind one engine-neutral core, and it is not tied to any application.

- **Remote nodes** run the database, its exporter, `node_exporter` and the lab collector, all provisioned by Ansible.
- **Your machine** runs Ansible, Prometheus, Grafana and JMeter.
- **Traffic** goes over Tailscale by default. Nodes can also use a private LAN or your own VPN, mixed per node. On unencrypted networks the lab enforces TLS. See [docs/architecture/networks.md](docs/architecture/networks.md).

```text
 LOCAL MACHINE                                      REMOTE NODES (x N, any DC)
 ┌──────────────────────────────┐                  ┌──────────────────────────────┐
 │ Ansible ── provision ────────┼── SSH ──────────▶│ MySQL | MariaDB | PostgreSQL │
 │ Prometheus ── scrape ────────┼── :19100/:19104 ▶│ node + engine exporter       │
 │ Grafana (dashboards)         │    /:19187/:19106│ lab collector, network probe │
 │ JMeter ──────────────────────┼── :13306/:15432 ▶│ chrony, nftables             │
 └──────────────────────────────┘                  └──────────────────────────────┘
```

## Engines and HA strategies

Pick one engine and one strategy per lab in `config/ha.yml` (`db_engine`, `ha_provider`).

| Engine | Strategy | Failover | Min nodes | Tested |
|---|---|---|---|---|
| MySQL 8.4 (8.0) | `async-replication` | manual (`make promote`) | 2 | containers, end to end |
| | `semi-sync` | manual | 2 | containers, end to end |
| | `orchestrator` | Orchestrator (raft) | 3 | containers, end to end |
| | `innodb-cluster` | Group Replication + MySQL Router | 3 (max 9) | containers, end to end |
| | `percona-xtradb-cluster` | Galera | 3 | containers: deploy and health only |
| MariaDB 11.4 (10.11, 11.8) | `async-replication` | manual | 2 | containers, end to end |
| | `semi-sync` | manual | 2 | containers, end to end |
| | `galera`, `maxscale` | | | planned, refused at validate time |
| PostgreSQL 17 (16, 18) | `streaming-replication` | manual, LSN-based, `pg_rewind` recovery | 2 | containers, end to end |
| | `sync-replication` | manual | 2 | containers, end to end |
| | `patroni` | | | planned, refused at validate time |

"End to end" means deploy, idempotent redeploy, health, switchover, unplanned failover, recover, ledger consistency and destroy, run against database containers with the Ansible Docker connection. None of it has run on a real Ubuntu host with systemd yet: package installs, service units and the firewall are untested there.

Each engine lives in `ansible/engines/<engine>/` (descriptor, roles, hooks). See [docs/architecture/engines.md](docs/architecture/engines.md) for the layout and how to add an engine, and [ha/README.md](ha/README.md) for the HA scenario runbooks of every engine. [docs/architecture/diagrams.md](docs/architecture/diagrams.md) has diagrams of the components, the engine abstraction, the lab lifecycle, failover and a benchmark run.

## Requirements

**On your machine** (any OS; `make validate-local` checks the tools, not the OS)
- Ansible core 2.17 or later (tested with 2.21)
- Docker with Compose v2
- Tailscale (only when some nodes are on a `tailscale` network), plus a route to every node network
- `python3`, `curl`, `openssl`, `nc`
- Java 8+ for JMeter (`make jmeter-install` downloads JMeter and the JDBC driver of the configured engine)

**Remote nodes**
- Ubuntu 22.04, Ubuntu 24.04 or Debian 12, with `python3` (Ansible needs it)
- MySQL: x86_64 only, because repo.mysql.com publishes amd64 packages only. MariaDB, PostgreSQL and the exporters also run on arm64.
- Reachable from this machine and from every other node: Tailscale with MagicDNS, a private network, or a VPN
- SSH access with a sudo-capable user (see Node user below)
- Outbound internet access to the engine's package repository (repo.mysql.com, repo.percona.com, deb.mariadb.org or apt.postgresql.org), GitHub releases and the distribution mirrors

**Node user**

Use the same user and key on every node, so the global `ssh:` block in `config/inventory.yml` covers them all. Create the key once on this machine, then run the second command once per node, logged in as any sudo user there (it asks for that user's password once):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/db-ha-lab_ed25519
```

```bash
KEY="$(cat ~/.ssh/db-ha-lab_ed25519.pub)"
ssh -t <existing-user>@<node> "sudo bash -c 'id db-ha-lab >/dev/null 2>&1 || adduser --disabled-password --gecos \"\" db-ha-lab; echo \"db-ha-lab ALL=(ALL) NOPASSWD:ALL\" > /etc/sudoers.d/db-ha-lab; chmod 440 /etc/sudoers.d/db-ha-lab; install -d -m700 -o db-ha-lab -g db-ha-lab /home/db-ha-lab/.ssh; echo \"$KEY\" > /home/db-ha-lab/.ssh/authorized_keys; chown db-ha-lab: /home/db-ha-lab/.ssh/authorized_keys; chmod 600 /home/db-ha-lab/.ssh/authorized_keys'"
```

`db-ha-lab` has no password (key login only) and passwordless sudo. For a node where you cannot create it, override per node: `ssh: {user: root}` under that node.

## Quick start

```bash
make deps                 # pinned Ansible collections into ansible/collections
make init                 # config/*.yml from the examples (gitignored)
$EDITOR config/inventory.yml config/ha.yml config/secrets.yml   # db_engine + ha_provider in ha.yml
ansible-vault encrypt config/secrets.yml
export ANSIBLE_VAULT_PASSWORD_FILE=~/.lab-vault-pass   # or: make <target> VAULT_ARGS=--ask-vault-pass

make validate             # local tools, config, secrets, addresses, SSH, sudo, node OS, full-mesh connectivity
make plan-host            # read-only: what provisioning would add to each node, and any conflicts
make provision            # chrony, firewall, the database server, accounts, exporters, lab collector, network probe
make monitoring           # targets, Prometheus + Grafana, local client probe
make metrics-check        # promtool, direct exporter checks, Prometheus target health

make ha HA=async-replication   # must match ha_provider in config/ha.yml
make topology                  # health

make jmeter-install
make benchmark SCENARIO=mixed-workload
make failover CONFIRM=yes      # failover experiment: RTO, RPO, consistency
```

Open Grafana at http://127.0.0.1:3000. The user is `admin` and the password is in `local/compose/.env`. The **Database HA Lab** folder holds eight dashboards: HA Overview, Latency Breakdown, Network, MySQL Performance, Replication & HA, Host & Storage, JMeter Benchmark, and Failover & Recovery. All except MySQL Performance use engine-neutral metrics; there is no MariaDB- or PostgreSQL-specific dashboard yet.

## Configuration

| File | Purpose |
|---|---|
| `config/inventory.yml` | Environment name, SSH settings, networks (tailscale / vpn / private), nodes (name, address, network, role, datacenter, optional overrides) |
| `config/ha.yml` | `db_engine`, `ha_provider`, the engine's section (`mysql:`, `mariadb:` or `postgres:`: version, port, server options), strategy settings, exporter ports |
| `config/monitoring.yml` | Scrape address mode, extra allowed sources (for example this machine's network), firewall behaviour, exporter versions |
| `config/secrets.yml` | Exporter, replication, benchmark and admin passwords as `db_<name>_password` (older `mysql_<name>_password` keys still work). Encrypt it with Ansible Vault. Environment variables `LAB_DB_*_PASSWORD` (or `LAB_MYSQL_*_PASSWORD`) take precedence. |
| `config/benchmark.yml` | Client location, endpoint, dataset, load defaults, per-scenario overrides |

The `lab_config` inventory plugin (`ansible/plugins/inventory/lab_config.py`) turns these files into the Ansible inventory, with every node in the `db_nodes` group (`mysql_nodes` is an alias). It validates them before any play runs and lists every problem it finds. It checks:
- the engine exists and supports the chosen strategy, and neither is planned
- the engine version
- unique names, addresses, server IDs and Tailscale IPs
- exactly one `primary`
- that example addresses have been replaced
- node counts per strategy
- port ranges and collisions
- that SSH key files exist

Run `make validate-config` for an offline check.

Example node with server overrides (the key is the engine's section name):

```yaml
nodes:
  - name: db-03
    address: db-03.tail1234.ts.net
    role: replica
    datacenter: ap-southeast
    server_id: 103
    mysql:                       # mariadb: or postgres: for those engines
      innodb_buffer_pool_size: 4G
```

## Ports

Every lab default differs from the product's standard port, so the lab can sit next to an existing installation. All are overridable in `config/ha.yml`.

| Component | Standard | Lab default |
|---|---|---|
| MySQL / MariaDB | 3306 | 13306 |
| PostgreSQL | 5432 | 15432 |
| mysqld_exporter / postgres_exporter | 9104 / 9187 | 19104 / 19187 |
| node_exporter / lab collector | 9100 / - | 19100 / 19106 |
| Orchestrator HTTP / raft | 3000 / 10008 | 13000 / 13008 |
| MySQL Router RW / RO | 6446 / 6447 | 16446 / 16447 |
| Galera gcomm / IST / SST | 4567 / 4568 / 4444 | 14567 / 14568 / 14444 |

The full table, including planned components, is in [docs/architecture/engines.md](docs/architecture/engines.md).

## Commands

```text
Configuration   make init | deps | validate | validate-local | validate-config
Infrastructure  make plan-host | provision | configure-exporters | collect-info
Monitoring      make targets | monitoring | monitoring-stop | monitoring-status | metrics-check | probes-start | probes-stop
Network         make mesh-check | network-check | network-matrix [IPERF=1]
HA              make ha HA=<strategy> | topology | switchover [NEW_PRIMARY=] | promote [NEW_PRIMARY=] [FORCE=1] | recover NODE=
Faults          make fault NODE= FAULT=stop-db|kill-db|isolate|netem [ACTION=restore] CONFIRM=yes
Benchmark       make jmeter-install | benchmark SCENARIO= [ENDPOINT=ha|primary|<node>] [J="k=v ..."] | failover CONFIRM=yes [FAULT=]
Reports         make report [RUN=<run_id>]
Status          make status
Teardown        make destroy [DATA=1]
Development     make lint | scenarios | dashboards
```

To pass extra Ansible arguments, use `ARGS`, for example `make provision ARGS="--limit db-02 --diff"`. `stop-mysql` and `kill-mysql` still work as aliases for `stop-db` and `kill-db`.

The scenarios are `baseline`, `read-heavy` (80/20), `write-heavy` (20/80), `mixed-workload` (50/50), `spike-test`, `soak-test`, `failover-test` and `network-disruption`. They use the same schema and deterministic dataset on every engine; the SQL differences live in `jmeter/scripts/dialects/<engine>.groovy`. For the run workflow, the output files and the RTO/RPO definitions, see [docs/runbooks/benchmarking.md](docs/runbooks/benchmarking.md).

## Safety

- **Existing services are protected.** `make plan-host` is read-only. For each node it shows the packages, services, users, paths and ports the lab would add, and `make provision` runs it first. Provisioning stops before changing anything if:
  - a lab port is already in use (it names the process);
  - a conflicting database server package is installed (each engine lists its own);
  - for MySQL, the InnoDB buffer pool would not fit in available memory (set `mysql.buffer_pool_ratio`, e.g. 0.25, on shared servers).

  An existing ntp, ntpsec or openntpd is kept, and chrony is installed only when none of them is present. Each engine's package repository is pinned to that engine's packages only.
- **Existing data is protected.** Provisioning refuses to touch a datadir or cluster the lab did not create. PostgreSQL uses its own cluster named `halab` and never touches other clusters. To take over an existing server anyway, pass `ARGS="-e mysql_adopt_existing=true"` (or `mariadb_adopt_existing`, `postgres_adopt_existing`). This changes its config and restarts it. It never deletes data.
- **Topology switches are explicit.** `make ha HA=x` refuses to run when `x` differs from `ha_provider` in `config/ha.yml`. Nodes record their deployed provider in a marker file, and deployment refuses a different provider until you run `make destroy`.
- **Replicas are never attached with divergent data.** MySQL and MariaDB refuse errant GTIDs; PostgreSQL refuses to replace a replica's data unless you pass `-e postgres_rebuild_replica=true`. A switchover fences the old primary before it promotes the new one, picks the most advanced candidate, and unfences the old primary if the promotion fails.
- **Faults need opt-in.** Every fault requires `CONFIRM=yes`. Isolation and netem also schedule a restore on the node itself.
- **Destructive targets need confirmation.** `make destroy` asks you to type the environment name. `DATA=1` also purges the server packages and the data.
- **Firewall.** By default the lab loads its own nftables table, `inet halab`, through `halab-firewall.service`. It covers only the lab ports. On each node's lab interface it accepts the node networks' `cidrs` plus `allowed_sources`, and drops everything else for those ports. Other ports and existing rules are untouched, and the host ruleset is never flushed. Remove it with `systemctl disable --now halab-firewall`. `firewall.backend: ufw` switches to host-wide ufw; `none` leaves the firewall to you.
- **Connectivity is verified before changes.** `make mesh-check` runs inside `validate`, `provision` and `ha`. It tests every node pair on the strategy's ports, logs in to the database over TLS, and checks this machine's access to the client and monitoring ports.
- **Secrets stay out of Git.** The real config files, the secrets file and the generated targets are all gitignored.
- **Root access.** Ansible connects to the database over the local socket as the OS superuser (`auth_socket` for MySQL/MariaDB, peer auth for PostgreSQL), so the lab never manages a database root password.

## Layout

```text
config/            user configuration (*.example.yml committed, real files gitignored)
ansible/           inventory plugin, engine-neutral playbooks and core roles (common, node-exporter, lab-collector)
ansible/engines/   one folder per engine: descriptor, roles and hooks (mysql, mariadb, postgres)
ha/<engine>/<strategy>/  architecture, runbooks and limitations per HA scenario
local/             Prometheus, Grafana (dashboards + generator), compose, helper scripts
metrics/           collector loader + one module per engine, metric schema, recording rules
jmeter/            scenarios (generated), Groovy sampler, per-engine SQL dialects and JDBC drivers, installer
benchmark/         runner, JTL exporter, correlation/analysis, consistency validation, report generator, results/
docs/              architecture, runbooks, metrics
```

See [docs/architecture/foundation.md](docs/architecture/foundation.md) for design decisions, assumptions and known limitations, and [docs/architecture/engines.md](docs/architecture/engines.md) for the engine layout and how to add an engine.
