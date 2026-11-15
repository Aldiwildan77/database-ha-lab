#!/usr/bin/env bash
# Starts/stops the local client-to-node probe exporter (metrics/collectors/local_probe.py) on :9465.
source "$(dirname "$0")/lib.sh"
pidfile="${LAB_ROOT}/local/state/local_probe.pid"
mkdir -p "${LAB_ROOT}/local/state"

case "${1:-}" in
  start)
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
      echo "local probe already running (pid $(cat "$pidfile"))"; exit 0
    fi
    [[ -f local/prometheus/targets/mysqld.json ]] || { yellow "no targets yet: run make targets"; exit 0; }
    port="$(lab_var mysql_port)"
    nohup python3 -I metrics/collectors/local_probe.py --targets local/prometheus/targets/mysqld.json \
      --port 9465 --mysql-port "${port:-3306}" > local/state/local_probe.log 2>&1 &
    echo $! > "$pidfile"
    green "local probe started on :9465 (pid $!)"
    ;;
  stop)
    if [[ -f "$pidfile" ]]; then
      kill "$(cat "$pidfile")" 2>/dev/null || true
      : > "$pidfile"
      echo "local probe stopped"
    fi
    ;;
  *) echo "usage: $0 start|stop"; exit 1 ;;
esac
