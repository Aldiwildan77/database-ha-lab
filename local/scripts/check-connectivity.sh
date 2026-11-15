#!/usr/bin/env bash
# Local-to-node checks: DNS, path (tailscale ping for tailscale nodes, ICMP otherwise), TCP ports.
source "$(dirname "$0")/lib.sh"
require python3 ansible-inventory nc ping

ports="${PORTS:-22 $(lab_var db_port) $(lab_var node_exporter_port) $(lab_var db_exporter_port) $(lab_var lab_collector_port)}"
fail=0
nodes="$(lab_nodes)"

if grep -q ' tailscale$' <<<"$nodes"; then
  require tailscale
  state="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["BackendState"])')"
  [[ "$state" == "Running" ]] || { red "local tailscale state: $state"; exit 1; }
  green "local tailscale: Running"
fi

while read -r name addr role dc ntype; do
  echo "== ${name} (${addr}, ${role}, ${dc}, ${ntype})"
  ip="$(python3 -c 'import socket,sys; print(socket.gethostbyname(sys.argv[1]))' "$addr" 2>/dev/null || true)"
  if [[ -z "$ip" ]]; then red "  dns: cannot resolve"; fail=1; continue; fi
  echo "  address: $ip"
  if [[ "$ntype" == "tailscale" ]]; then
    if pong="$(tailscale ping --c 3 --timeout 5s "$addr" 2>&1 | grep 'pong' | tail -1)" && [[ -n "$pong" ]]; then
      echo "  tailscale: ${pong}"
    else
      red "  tailscale: no pong"; fail=1
    fi
  elif rtt="$(ping -c 3 "$ip" 2>/dev/null | tail -1)" && [[ "$rtt" == *"/"* ]]; then
    echo "  icmp: ${rtt}"
  else
    yellow "  icmp: no reply (may be filtered)"
  fi
  for p in $ports; do
    if nc -z -G 3 -w 3 "$ip" "$p" >/dev/null 2>&1 || nc -z -w 3 "$ip" "$p" >/dev/null 2>&1; then
      echo "  tcp/${p}: open"
    else
      yellow "  tcp/${p}: closed or filtered"
    fi
  done
done <<<"$nodes"

exit "$fail"
