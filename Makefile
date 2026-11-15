SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

export ANSIBLE_CONFIG := $(CURDIR)/ansible.cfg

PLAYBOOK  ?= ansible-playbook
COMPOSE   ?= docker compose -f local/compose/docker-compose.yml --env-file local/compose/.env
VAULT_ARGS ?=
ARGS      ?=

HA_PROVIDERS := async-replication orchestrator semi-sync innodb-cluster percona-xtradb-cluster
SCENARIOS    := baseline read-heavy write-heavy mixed-workload spike-test soak-test failover-test network-disruption
CONFIG_FILES := inventory ha monitoring benchmark

ansible = $(PLAYBOOK) $(VAULT_ARGS) $(ARGS)

.PHONY: help init deps validate validate-config provision configure-exporters collect-info \
        targets monitoring monitoring-stop monitoring-status metrics-check \
        network-check network-matrix ha benchmark failover report status destroy lint

help: ## Show targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

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

validate: validate-config ## Validate config, secrets, Tailscale, DNS, SSH, OS
	$(ansible) ansible/playbooks/validate.yml

provision: validate-config ## Install MySQL + exporters on all nodes
	$(ansible) ansible/playbooks/provision.yml

configure-exporters: validate-config ## (Re)install exporters only
	$(ansible) ansible/playbooks/configure-exporters.yml

collect-info: validate-config ## Gather node specs into local/state/node-info.json
	$(ansible) ansible/playbooks/collect-info.yml

targets: validate-config ## Generate Prometheus targets from the inventory
	local/scripts/generate-targets.sh $(ARGS)

local/compose/.env:
	@umask 077; printf 'GRAFANA_ADMIN_PASSWORD=%s\nPROMETHEUS_RETENTION=30d\n' "$$(openssl rand -base64 24 | tr -d '/+=')" > $@
	@echo "Generated $@ (Grafana admin password inside)"

monitoring: local/compose/.env targets ## Start local Prometheus + Grafana
	$(COMPOSE) up -d
	@echo "Prometheus: http://127.0.0.1:9090   Grafana: http://127.0.0.1:3000 (admin / see local/compose/.env)"

monitoring-stop: ## Stop local Prometheus + Grafana (data volumes kept)
	$(COMPOSE) down

monitoring-status: local/compose/.env ## Show monitoring containers
	$(COMPOSE) ps

metrics-check: local/compose/.env ## Validate Prometheus config and check exporters/targets
	$(COMPOSE) run --rm --no-deps --entrypoint promtool prometheus check config /etc/prometheus/prometheus.yml
	local/scripts/health-check.sh

network-check: validate-config ## Local-to-node DNS, tailscale ping and port checks
	local/scripts/check-connectivity.sh

network-matrix: ## Node-to-node RTT matrix (Phase 5)
	local/scripts/network-matrix.sh

ha: validate-config ## Deploy HA topology: make ha HA=<provider>
	@[ -n "$(HA)" ] || { echo "usage: make ha HA=<$(HA_PROVIDERS)>"; exit 1; }
	@echo " $(HA_PROVIDERS) " | grep -q " $(HA) " || { echo "unknown HA provider '$(HA)'. Valid: $(HA_PROVIDERS)"; exit 1; }
	@cfg="$$(local/scripts/lab-var.sh ha_provider)"; \
	  [ "$$cfg" = "$(HA)" ] || { echo "HA=$(HA) differs from config/ha.yml ha_provider=$$cfg. Switching topologies needs an explicit cleanup (make destroy) and a config change."; exit 1; }
	@echo "HA deployment for $(HA) is not implemented yet (async-replication is next, Phase 4)."; exit 2

benchmark: ## Run a JMeter scenario: make benchmark SCENARIO=<name>
	@[ -n "$(SCENARIO)" ] || { echo "usage: make benchmark SCENARIO=<$(SCENARIOS)>"; exit 1; }
	@echo " $(SCENARIOS) " | grep -q " $(SCENARIO) " || { echo "unknown scenario '$(SCENARIO)'. Valid: $(SCENARIOS)"; exit 1; }
	@echo "Benchmarking is not implemented yet (Phase 6)."; exit 2

failover: ## Controlled failover experiment (Phase 4+)
	@echo "Failover experiments are not implemented yet (Phase 4)."; exit 2

report: ## Generate benchmark report (Phase 8)
	@echo "Reporting is not implemented yet (Phase 8)."; exit 2

status: validate-config ## Inventory, monitoring and exporter health
	@ansible-inventory --graph
	@$(MAKE) --no-print-directory monitoring-status || true
	@local/scripts/health-check.sh || true

destroy: ## Remove the HA topology (Phase 4; requires confirmation)
	@echo "This will remove HA configuration from all nodes. Type the environment name to confirm:"; \
	  read -r answer; env="$$(local/scripts/lab-var.sh lab_environment)"; \
	  [ -n "$$env" ] && [ "$$answer" = "$$env" ] || { echo "aborted"; exit 1; }; \
	  echo "destroy-ha is not implemented yet (Phase 4). Nothing was changed."; exit 2

lint: ## ansible-lint, shellcheck, compose config
	ansible-lint ansible/
	shellcheck -x -e SC1091 local/scripts/*.sh
	$(COMPOSE) config -q
