#!/usr/bin/env bash
# Checks exporters directly and Prometheus target health.
source "$(dirname "$0")/lib.sh"
require curl python3

prom="http://127.0.0.1:${PROMETHEUS_PORT:-9090}"
fail=0

for t in node db; do
  [[ -f "local/prometheus/targets/${t}.json" ]] || { red "no ${t} targets yet: run make targets"; exit 1; }
done

echo "== Exporters (direct)"
while read -r node addr; do
  if curl -fsS --max-time 5 "http://${addr}/metrics" -o /dev/null; then
    green "  node_exporter   ${node} ${addr} ok"
  else
    red "  node_exporter   ${node} ${addr} unreachable"; fail=1
  fi
done < <(lab_target_addresses node)
exporter="$(lab_var db_exporter_name)"; up_metric="$(lab_var db_exporter_up_metric)"
while read -r node addr; do
  body="$(curl -fsS --max-time 10 "http://${addr}/metrics" 2>/dev/null || true)"
  if grep -q "^${up_metric} 1" <<<"$body"; then
    green "  ${exporter} ${node} ${addr} ok (${up_metric} 1)"
  elif [[ -n "$body" ]]; then
    red "  ${exporter} ${node} ${addr} reachable but ${up_metric} != 1"; fail=1
  else
    red "  ${exporter} ${node} ${addr} unreachable"; fail=1
  fi
done < <(lab_target_addresses db)

echo "== Prometheus targets (${prom})"
if targets="$(curl -fsS --max-time 5 "${prom}/api/v1/targets?state=active" 2>/dev/null)"; then
  python3 -c '
import json, sys
bad = 0
for t in json.load(sys.stdin)["data"]["activeTargets"]:
    l = t["labels"]
    job, node = l.get("job", ""), l.get("node", l.get("instance", ""))
    ok = t["health"] == "up"
    if not ok and job == "benchmark":
        # JMeter exposes metrics only while a benchmark runs.
        print("  {:10} {:20} idle (no benchmark running)".format(job, node))
        continue
    bad += not ok
    err = "" if ok else " " + t.get("lastError", "")
    print("  {:10} {:20} {}{}".format(job, node, t["health"], err))
sys.exit(1 if bad else 0)
' <<<"$targets" || fail=1
else
  yellow "  Prometheus not reachable; run make monitoring"; fail=1
fi

exit "$fail"
