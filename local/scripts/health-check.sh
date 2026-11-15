#!/usr/bin/env bash
# Checks exporters directly and Prometheus target health.
source "$(dirname "$0")/lib.sh"
require curl python3

prom="http://127.0.0.1:${PROMETHEUS_PORT:-9090}"
fail=0

[[ -f local/prometheus/targets/node.json ]] || { red "no targets yet: run make targets"; exit 1; }

echo "== Exporters (direct)"
while read -r node addr; do
  if curl -fsS --max-time 5 "http://${addr}/metrics" -o /dev/null; then
    green "  node_exporter   ${node} ${addr} ok"
  else
    red "  node_exporter   ${node} ${addr} unreachable"; fail=1
  fi
done < <(lab_target_addresses node)
while read -r node addr; do
  body="$(curl -fsS --max-time 10 "http://${addr}/metrics" 2>/dev/null || true)"
  if grep -q '^mysql_up 1' <<<"$body"; then
    green "  mysqld_exporter ${node} ${addr} ok (mysql_up 1)"
  elif [[ -n "$body" ]]; then
    red "  mysqld_exporter ${node} ${addr} reachable but mysql_up != 1"; fail=1
  else
    red "  mysqld_exporter ${node} ${addr} unreachable"; fail=1
  fi
done < <(lab_target_addresses mysqld)

echo "== Prometheus targets (${prom})"
if targets="$(curl -fsS --max-time 5 "${prom}/api/v1/targets?state=active" 2>/dev/null)"; then
  python3 -c '
import json, sys
bad = 0
for t in json.load(sys.stdin)["data"]["activeTargets"]:
    l = t["labels"]
    ok = t["health"] == "up"
    bad += not ok
    err = "" if ok else " " + t.get("lastError", "")
    print(f"  {l.get(\"job\"):10} {l.get(\"node\", l.get(\"instance\")):20} {t[\"health\"]}{err}")
sys.exit(1 if bad else 0)
' <<<"$targets" || fail=1
else
  yellow "  Prometheus not reachable; run make monitoring"; fail=1
fi

exit "$fail"
