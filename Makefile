SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

export ANSIBLE_CONFIG := $(CURDIR)/ansible.cfg

PLAYBOOK  ?= ansible-playbook
COMPOSE   ?= docker compose -f local/compose/docker-compose.yml --env-file local/compose/.env
VAULT_ARGS ?=
ARGS      ?=

HA_PROVIDERS := async-replication orchestrator semi-sync innodb-cluster percona-xtradb-cluster galera streaming-replication sync-replication patroni
SCENARIOS    := baseline read-heavy write-heavy mixed-workload spike-test soak-test failover-test network-disruption
CONFIG_FILES := inventory ha monitoring benchmark

ansible = $(PLAYBOOK) $(VAULT_ARGS) $(ARGS)

.PHONY: help manual screenshots pdf init deps validate validate-local validate-config plan-host provision configure-exporters collect-info \
        targets monitoring monitoring-stop monitoring-status metrics-check mesh-check network-check network-matrix \
        probes-start probes-stop ha topology switchover promote recover fault jmeter-install scenarios \
        dashboards benchmark failover report status destroy lint

help: ## Show targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---------------------------------------------------------------- Configuration
init: ## Create config/*.yml from the examples (never overwrites)
	@for f in $(CONFIG_FILES) secrets; do \
	  if [ -f config/$$f.yml ]; then echo "config/$$f.yml exists, kept"; \
	  else cp config/$$f.example.yml config/$$f.yml && echo "created config/$$f.yml"; fi; \
	done
	@echo "Next: edit config/inventory.yml and config/secrets.yml, then: ansible-vault encrypt config/secrets.yml"

deps: ## Install pinned Ansible collections into ansible/collections
	ansible-galaxy collection install -r requirements.yml -p ansible/collections

validate-config: ## Validate config files only (offline)
	@ansible-inventory --graph >/dev/null && echo "configuration OK" && ansible-inventory --graph

validate-local: validate-config ## Check this machine's tools (Ansible, Python, Docker Compose, Java, Tailscale...)
	$(ansible) ansible/playbooks/validate.yml --tags tools

validate: validate-config ## Validate local tools, config, secrets, Tailscale, DNS, SSH, node OS
	$(ansible) ansible/playbooks/validate.yml

# ---------------------------------------------------------------- Infrastructure
plan-host: validate-config ## Read-only: what provisioning would change on each node, and conflicts
	$(ansible) ansible/playbooks/check-host.yml

provision: validate-config ## Install the database engine + exporters + collectors on all nodes (runs plan-host first)
	$(ansible) ansible/playbooks/provision.yml

configure-exporters: validate-config ## (Re)install exporters and lab collectors only
	$(ansible) ansible/playbooks/configure-exporters.yml

collect-info: validate-config ## Gather node specs into local/state/node-info.json
	$(ansible) ansible/playbooks/collect-info.yml

# ---------------------------------------------------------------- Monitoring
targets: validate-config ## Generate Prometheus targets from the inventory
	local/scripts/generate-targets.sh $(ARGS)

local/compose/.env:
	@umask 077; printf 'GRAFANA_ADMIN_PASSWORD=%s\nPROMETHEUS_RETENTION=30d\n' "$$(openssl rand -base64 24 | tr -d '/+=')" > $@
	@echo "Generated $@ (Grafana admin password inside)"

monitoring: local/compose/.env targets ## Start local Prometheus + Grafana + client probe
	$(COMPOSE) up -d
	@local/scripts/probes.sh start
	@echo "Prometheus: http://127.0.0.1:9090   Grafana: http://127.0.0.1:3000 (admin / see local/compose/.env)"

monitoring-stop: ## Stop local Prometheus + Grafana + probe (data volumes kept)
	@local/scripts/probes.sh stop
	$(COMPOSE) down

monitoring-status: local/compose/.env ## Show monitoring containers
	$(COMPOSE) ps

metrics-check: local/compose/.env ## Validate Prometheus config and check exporters/targets
	$(COMPOSE) run --rm --no-deps --entrypoint promtool prometheus check config /etc/prometheus/prometheus.yml
	local/scripts/health-check.sh

probes-start: ## Start the local client-to-node probe exporter (:9465)
	@local/scripts/probes.sh start

probes-stop: ## Stop the local probe exporter
	@local/scripts/probes.sh stop

# ---------------------------------------------------------------- Network
mesh-check: validate-config ## Full-mesh TCP + database TLS login between nodes, and this machine to nodes
	$(ansible) ansible/playbooks/check-mesh.yml

network-check: validate-config ## Local-to-node DNS, tailscale ping and port checks
	local/scripts/check-connectivity.sh

network-matrix: validate-config ## Node-to-node RTT/loss/path matrix (IPERF=1 adds throughput)
	$(ansible) ansible/playbooks/network-matrix.yml $(if $(IPERF),-e iperf=true)

# ---------------------------------------------------------------- HA
ha: validate-config ## Deploy HA topology from config/ha.yml ha_provider (HA=<provider> must match it if given)
	@cfg="$$(local/scripts/lab-var.sh ha_provider)"; ha="$${HA:-$$cfg}"; \
	  echo " $(HA_PROVIDERS) " | grep -q " $$ha " || { echo "unknown HA provider '$$ha'. Valid: $(HA_PROVIDERS)"; exit 1; }; \
	  [ "$$cfg" = "$$ha" ] || { echo "HA=$$ha differs from config/ha.yml ha_provider=$$cfg. Switching topologies needs make destroy and a config change."; exit 1; }
	$(ansible) ansible/playbooks/deploy-ha.yml

topology: validate-config ## Show topology and health
	$(ansible) ansible/playbooks/topology.yml

switchover: validate-config ## Planned primary change: make switchover [NEW_PRIMARY=<node>]
	$(ansible) ansible/playbooks/failover.yml -e failover_mode=switchover $(if $(NEW_PRIMARY),-e new_primary=$(NEW_PRIMARY))

promote: validate-config ## Unplanned failover after primary loss: make promote [NEW_PRIMARY=<node>] [FORCE=1]
	$(ansible) ansible/playbooks/failover.yml -e failover_mode=failover $(if $(NEW_PRIMARY),-e new_primary=$(NEW_PRIMARY)) $(if $(FORCE),-e force=true)

recover: validate-config ## Bring a failed node back: make recover NODE=<node> [REBUILD=1 clones it from the primary]
	@[ -n "$(NODE)" ] || { echo "usage: make recover NODE=<node> [REBUILD=1]"; exit 1; }
	$(ansible) ansible/playbooks/recover.yml -e node=$(NODE) $(if $(REBUILD),-e rebuild=true)

fault: validate-config ## make fault NODE=<n> FAULT=<stop-db|kill-db|isolate|netem> [ACTION=restore] CONFIRM=yes
	@[ "$(CONFIRM)" = yes ] || { echo "Faults disrupt the cluster. Add CONFIRM=yes."; exit 1; }
	@[ -n "$(NODE)" ] && [ -n "$(FAULT)" ] || { echo "usage: make fault NODE=<node> FAULT=<type> [ACTION=inject|restore] CONFIRM=yes"; exit 1; }
	$(ansible) ansible/playbooks/fault.yml -e node=$(NODE) -e fault=$(FAULT) -e action=$(or $(ACTION),inject) -e confirm=yes $(if $(NETEM),-e '{"netem": "$(NETEM)"}')

# ---------------------------------------------------------------- Benchmark
jmeter-install: ## Install JMeter 5.6.3 + the JDBC driver of the configured db_engine into local/tools
	jmeter/install.sh $$(local/scripts/lab-var.sh db_engine 2>/dev/null || echo mysql)

scenarios: ## Regenerate jmeter/scenarios/*.jmx
	python3 -I jmeter/build-scenarios.py

dashboards: ## Regenerate local/grafana/dashboards/*.json
	python3 -I local/grafana/build-dashboards.py

benchmark: validate-config ## make benchmark SCENARIO=<name> [ENDPOINT=ha|primary|<node>] [J="threads=32 duration=600"] [FAULT=.. CONFIRM=yes]
	@[ -n "$(SCENARIO)" ] || { echo "usage: make benchmark SCENARIO=<$(SCENARIOS)>"; exit 1; }
	@echo " $(SCENARIOS) " | grep -q " $(SCENARIO) " || { echo "unknown scenario '$(SCENARIO)'. Valid: $(SCENARIOS)"; exit 1; }
	VAULT_ARGS="$(VAULT_ARGS)" python3 -I benchmark/runner/run.py --scenario $(SCENARIO) \
	  $(if $(ENDPOINT),--endpoint $(ENDPOINT)) $(foreach p,$(J),-J $(p)) \
	  $(if $(FAULT),--fault $(FAULT)) $(if $(CONFIRM),--confirm $(CONFIRM)) $(RUN_ARGS)

failover: validate-config ## Failover experiment under load: make failover CONFIRM=yes [FAULT=stop-db|kill-db|isolate]
	@[ "$(CONFIRM)" = yes ] || { echo "The failover experiment stops the primary. Add CONFIRM=yes."; exit 1; }
	VAULT_ARGS="$(VAULT_ARGS)" python3 -I benchmark/runner/run.py --scenario failover-test \
	  --fault $(or $(FAULT),stop-db) --confirm yes --endpoint $(or $(ENDPOINT),ha) $(foreach p,$(J),-J $(p)) $(RUN_ARGS)

screenshots: ## Capture every Grafana panel for a run: make screenshots [RUN=<run_id>]
	python3 -I benchmark/runner/screenshots.py $(if $(RUN),--run $(RUN))

pdf: ## Report PDF of a run into reports/<db>/<ha>/<scenario>/ (run make screenshots first): make pdf [RUN=<run_id>]
	python3 -I benchmark/report/pdf.py $(if $(RUN),--run $(RUN))

report: ## Rebuild a report: make report [RUN=<run_id>] (default: latest)
	@run="$(or $(RUN),$$(ls -1d benchmark/results/run-* 2>/dev/null | sort | tail -1 | xargs -n1 basename))"; \
	  [ -n "$$run" ] || { echo "no runs in benchmark/results"; exit 1; }; \
	  python3 -I benchmark/report/report.py benchmark/results/$$run && echo "benchmark/results/$$run/report.md"

# ---------------------------------------------------------------- Status / teardown
status: validate-config ## Inventory, monitoring, exporter health and topology
	@ansible-inventory --graph
	@$(MAKE) --no-print-directory monitoring-status || true
	@local/scripts/health-check.sh || true
	@$(ansible) ansible/playbooks/topology.yml || true

destroy: validate-config ## Remove the HA topology (asks for confirmation; DATA=1 also purges the database packages and data)
	@env="$$(local/scripts/lab-var.sh lab_environment)"; prov="$$(local/scripts/lab-var.sh ha_provider)"; \
	  echo "This removes the $$prov topology from all nodes$(if $(DATA), AND DELETES ALL DATABASE DATA AND PACKAGES)."; \
	  printf "Type the environment name (%s) to confirm: " "$$env"; read -r answer; \
	  [ -n "$$env" ] && [ "$$answer" = "$$env" ] || { echo "aborted"; exit 1; }; \
	  $(ansible) ansible/playbooks/destroy-ha.yml -e confirm=$$env $(if $(DATA),-e destroy_data=true)

manual: ## Rebuild the PDF manual (docs/manuals) with headless Chrome
	docs/manuals/build.sh

lint: ## ansible-lint, yamllint, shellcheck, compose config, python compile
	ansible-lint ansible/
	yamllint -s ansible/engines ansible/playbooks ansible/roles ansible/tasks ansible/inventory config local metrics
	shellcheck -x -e SC1091 local/scripts/*.sh jmeter/install.sh
	$(COMPOSE) config -q
	python3 -I -m py_compile metrics/collectors/*.py benchmark/*/*.py jmeter/build-scenarios.py local/grafana/build-dashboards.py
