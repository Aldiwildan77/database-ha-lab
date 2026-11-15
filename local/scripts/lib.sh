#!/usr/bin/env bash
# Shared helpers. Source from other scripts.
set -euo pipefail

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export ANSIBLE_CONFIG="${LAB_ROOT}/ansible.cfg"
cd "${LAB_ROOT}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

require() {
  for bin in "$@"; do
    command -v "$bin" >/dev/null 2>&1 || { red "missing required command: $bin"; exit 1; }
  done
}

# Prints one line per node: name address role datacenter network_type
lab_nodes() {
  ansible-inventory --list 2>/dev/null | python3 -c '
import json, sys
inv = json.load(sys.stdin)
hv = inv.get("_meta", {}).get("hostvars", {})
for h in inv.get("db_nodes", {}).get("hosts", []) or sorted(
        {x for g in inv.get("db_nodes", {}).get("children", []) for x in inv.get(g, {}).get("hosts", [])}):
    v = hv[h]
    val = lambda x: x.get("__ansible_unsafe", x) if isinstance(x, dict) else x
    print(h, *(val(v[k]) for k in ("node_address", "node_role", "node_datacenter", "node_network_type")))
'
}

# Prints "node address" from the generated Prometheus targets (make targets).
lab_target_addresses() {
  local job="$1"
  python3 -c '
import json, sys
for t in json.load(open(sys.argv[1])):
    print(t["labels"]["node"], t["targets"][0])
' "${LAB_ROOT}/local/prometheus/targets/${job}.json"
}

# Prints a lab-wide variable (e.g. ha_provider, lab_environment) from the inventory.
lab_var() {
  local first
  first="$(lab_nodes | awk 'NR==1 {print $1}')"
  ansible-inventory --host "$first" 2>/dev/null | python3 -c '
import json, sys
v = json.load(sys.stdin)[sys.argv[1]]
print(v.get("__ansible_unsafe", v) if isinstance(v, dict) else v)
' "$1"
}
