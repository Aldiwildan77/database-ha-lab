# Diagrams

Mermaid diagrams (GitHub renders them inline), neutral grey theme. Names match files in the repo.

## 1. Components

What runs where. The operator's machine drives everything; nodes only run the database stack.

```mermaid
%%{init: {'theme': 'neutral'}}%%
flowchart LR
  subgraph OP["Operator machine (macOS / Linux)"]
    MK["make targets"]
    CFG["config/*.yml<br/>inventory, ha, secrets, monitoring, benchmark"]
    INV["lab_config inventory plugin"]
    ANS["ansible-playbook<br/>ansible/playbooks/*"]
    RUN["benchmark/runner/run.py"]
    JM["JMeter + JDBC driver<br/>jmeter/scenarios/*.jmx"]
    REP["benchmark/report/report.py"]
    subgraph DC["Docker Compose (local/compose)"]
      PROM["Prometheus"]
      GRAF["Grafana"]
    end
  end

  subgraph NODE["Each database node (Ubuntu / Debian)"]
    DB[("Database<br/>MySQL / MariaDB / PostgreSQL")]
    HA["HA software<br/>Orchestrator, Router, Galera, Patroni+etcd"]
    EXP["node_exporter<br/>mysqld / postgres_exporter"]
    COL["ha-lab collector"]
    FW["halab nftables table"]
  end

  MK --> ANS
  MK --> RUN
  CFG --> INV --> ANS
  ANS -->|"SSH + sudo"| NODE
  RUN --> ANS
  RUN --> JM
  JM -->|"JDBC, lab port"| DB
  PROM -->|"scrape"| EXP
  PROM -->|"scrape"| COL
  RUN -->|"JTL exporter :9464"| PROM
  GRAF --> PROM
  RUN --> REP
  REP -->|"PromQL"| PROM
  DB <-->|"replication / group comms"| DB
  HA --- DB
  COL --- DB
```

## 2. Engine abstraction

The core never names a database. It reads the engine descriptor and calls hooks by name.

```mermaid
%%{init: {'theme': 'neutral'}}%%
flowchart TB
  HAY["config/ha.yml<br/>db_engine, ha_provider, engine section"] --> PLUG
  DESC["ansible/engines/&lt;engine&gt;/engine.yml<br/>ports, packages, versions, architectures,<br/>service_unit, exporter, collector,<br/>ha.&lt;strategy&gt;: family, failover, min/max nodes,<br/>mesh/client ports, provision, deploy, status"] --> PLUG
  PLUG["lab_config inventory plugin<br/>validates config + descriptor,<br/>refuses planned strategies,<br/>resolves ports"] --> VARS["host vars<br/>db_engine, db_port, lab_engine,<br/>lab_ha_strategy, lab_engine_tasks, ..."]

  VARS --> CORE
  subgraph CORE["Core playbooks (ansible/playbooks)"]
    P1["provision.yml"]
    P2["deploy-ha.yml"]
    P3["failover.yml"]
    P4["recover.yml"]
    P5["destroy-ha.yml"]
    P6["topology.yml / check-mesh.yml / consistency.yml"]
  end

  P1 -->|"loop ha.&lt;strategy&gt;.provision"| ROLES
  P2 -->|"loop ha.&lt;strategy&gt;.deploy"| ROLES
  P3 & P4 & P5 & P6 -->|"include_tasks lab_engine_tasks/&lt;hook&gt;.yml"| HOOKS

  subgraph ENG["ansible/engines/&lt;engine&gt;/"]
    ROLES["roles/<br/>mysql, replication, orchestrator, innodb-cluster, percona-xtradb-cluster<br/>mariadb, mariadb-replication<br/>postgres, postgres-replication, postgres-exporter"]
    HOOKS["tasks/ hooks<br/>topology-facts, ha-health,<br/>failover-choose / promote / unfence / repoint,<br/>switchover-managed, recover-rejoin,<br/>destroy-teardown / purge, mesh-login,<br/>ledger-dump, node-summary, server-version"]
  end

  subgraph SHARED["Shared roles (ansible/roles)"]
    R1["common: packages, chrony, firewall"]
    R2["node-exporter"]
    R3["lab-collector"]
  end
  P1 --> SHARED
```

## 3. Lab lifecycle

```mermaid
%%{init: {'theme': 'neutral'}}%%
flowchart LR
  A["make deps<br/>make jmeter-install"] --> B["make validate<br/>tools, config, SSH, sudo, OS, arch"]
  B --> C["make plan-host<br/>read-only conflict check"]
  C --> D["make provision<br/>engine + exporters + collector + firewall"]
  D --> E["make ha HA=&lt;strategy&gt;<br/>build topology"]
  E --> F["make topology<br/>health"]
  F --> G["make monitoring<br/>make metrics-check"]
  G --> H["make benchmark SCENARIO=...<br/>make failover"]
  H --> I["make report"]
  F --> S["make switchover / promote / recover"]
  S --> F
  I --> Z["make destroy [DATA=1]"]
  Z -. "change engine or strategy" .-> D
```

## 4. Manual failover (async-replication, semi-sync, streaming, sync)

`failover.yml -e failover_mode=switchover|failover`. Strategies with an HA manager (Orchestrator, InnoDB Cluster, Galera) go to `switchover-managed` instead.

```mermaid
%%{init: {'theme': 'neutral'}}%%
sequenceDiagram
  autonumber
  participant Op as Operator / run.py
  participant FO as failover.yml
  participant H as engine hooks
  participant Old as old primary
  participant C as candidates

  Op->>FO: make switchover NEW_PRIMARY=x / make promote [FORCE=1]
  FO->>H: topology-facts (every node)
  FO->>FO: refuse if switchover and primary down,<br/>or failover and primary up without FORCE
  FO->>H: failover-choose
  H->>Old: fence (read_only / super_read_only, or clean stop for PostgreSQL)
  H->>C: wait until each applied the target position (apply_timeout)
  H->>H: pick a caught-up candidate that is not behind any other
  alt no safe candidate
    FO->>H: failover-unfence
    H->>Old: make writable again
    FO-->>Op: fail with positions
  else candidate chosen
    FO->>H: failover-promote
    H->>C: new primary writable (STOP/RESET replica, pg_promote)
    FO->>H: failover-repoint
    H->>C: other nodes (and fenced old primary) follow the new primary
    FO-->>Op: events in local/state/failover-*.json
  end
```

## 5. Benchmark run

`make benchmark SCENARIO=...` and `make failover` both run `benchmark/runner/run.py`.

```mermaid
%%{init: {'theme': 'neutral'}}%%
sequenceDiagram
  autonumber
  participant R as run.py
  participant A as Ansible
  participant J as JMeter
  participant P as Poller (topology every 0.5 s)
  participant DB as nodes

  R->>A: benchmark-context.yml (topology, versions, endpoint)
  R->>DB: client network probe + network-matrix.yml
  R->>J: prepare.jmx (dataset, rows + seed)
  R->>P: start, wait until converged
  R->>J: scenario .jmx (JTL to results/, exporter to Prometheus)
  opt fault (failover-test, network-disruption)
    R->>A: fault.yml inject (stop-db, kill-db, isolate, netem)
    P-->>R: primary_unavailable, new primary, recovered
    opt manual strategy
      R->>A: failover.yml promote after manual_promotion_delay_seconds
    end
    R->>A: fault.yml restore, recover.yml
  end
  J-->>R: finished
  R->>A: consistency.yml (ledger per node: lost acknowledged writes)
  R->>R: results.json, failover-events.json
  R->>R: report.md (RPO, RTO, latency, Prometheus summary)
  Note over R: make report rebuilds a report later
```
