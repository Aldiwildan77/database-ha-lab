#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
require ansible-playbook
ansible-playbook ansible/playbooks/generate-targets.yml "$@"
for job in node mysqld; do
  green "local/prometheus/targets/${job}.json:"
  lab_target_addresses "$job" | sed 's/^/  /'
done
