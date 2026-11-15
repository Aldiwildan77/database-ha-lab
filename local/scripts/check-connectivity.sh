#!/usr/bin/env bash
# Local-to-node checks over Tailscale: DNS, tailscale ping (path + latency), TCP ports.
source "$(dirname "$0")/lib.sh"
require tailscale python3 ansible-inventory nc

ports="${PORTS:-22 3306 9100 9104}"
fail=0

state="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["BackendState"])')"
[[ "$state" == "Running" ]] || { red "local tailscale state: $state"; exit 1; }
green "local tailscale: Running"

while read -r name fqdn role dc; do
  echo "== ${name} (${fqdn}, ${role}, ${dc})"
  ip="$(python3 -c 'import socket,sys; print(socket.gethostbyname(sys.argv[1]))' "$fqdn" 2>/dev/null || true)"
  if [[ -z "$ip" ]]; then red "  dns: cannot resolve"; fail=1; continue; fi
  case "$ip" in
    100.*) echo "  dns: $ip" ;;
    *) yellow "  dns: $ip (not a Tailscale address)"; fail=1 ;;
  esac
  if pong="$(tailscale ping --c 3 --timeout 5s "$fqdn" 2>&1 | grep 'pong' | tail -1)" && [[ -n "$pong" ]]; then
    echo "  tailscale: ${pong}"
  else
    red "  tailscale: no pong"; fail=1
  fi
  for p in $ports; do
    if nc -z -G 3 -w 3 "$ip" "$p" >/dev/null 2>&1 || nc -z -w 3 "$ip" "$p" >/dev/null 2>&1; then
      echo "  tcp/${p}: open"
    else
      yellow "  tcp/${p}: closed or filtered"
    fi
  done
done < <(lab_nodes)

exit "$fail"
