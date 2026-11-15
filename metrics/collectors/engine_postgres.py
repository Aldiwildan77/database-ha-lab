#!/usr/bin/env python3
"""PostgreSQL engine collector: replication and HA state.

Loaded by ha_lab_collector.py when the config says engine=postgres. Contract for every engine module:
Collector(cfg) with .lock and .collect(registry), emitting at least ha_lab_db_up, ha_lab_writable,
ha_lab_read_only and ha_lab_replication_position.

- ha_lab_replication_position: WAL position as a byte offset (replay LSN on a standby, insert LSN on a primary).
  Equal values across nodes indicate convergence.
- ha_lab_read_only / ha_lab_writable: from pg_is_in_recovery() and default_transaction_read_only (fence).
- replication_lag_seconds: standby side, now() minus the commit time of the last replayed transaction (0 when idle).
- replication_queue_size{queue="wal_replay"}: WAL bytes received but not yet replayed (this engine's unit is bytes).
- ha_lab_sync_standbys: synchronous or quorum standbys seen by a primary.

Config: /etc/ha-lab/collector.json (user, password, host = socket directory or hostname, db_port).
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import promlib  # noqa: E402

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

STATE_SQL = """
SELECT pg_is_in_recovery() AS in_recovery,
       current_setting('default_transaction_read_only') = 'on' AS fenced,
       pg_wal_lsn_diff(CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn() ELSE pg_current_wal_insert_lsn() END, '0/0')::float8 AS position,
       pg_wal_lsn_diff(pg_last_wal_receive_lsn(), '0/0')::float8 AS received,
       pg_wal_lsn_diff(pg_last_wal_replay_lsn(), '0/0')::float8 AS replayed,
       CASE WHEN pg_is_in_recovery() THEN pg_is_wal_replay_paused() ELSE false END AS paused,
       EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp())::float8 AS replay_age,
       (SELECT status FROM pg_stat_wal_receiver LIMIT 1) AS receiver_status,
       COALESCE((SELECT received_tli FROM pg_stat_wal_receiver), (SELECT timeline_id FROM pg_control_checkpoint())) AS timeline,
       (SELECT count(*) FROM pg_stat_replication WHERE state = 'streaming')::int AS streaming,
       (SELECT count(*) FROM pg_stat_replication WHERE sync_state IN ('sync', 'quorum'))::int AS sync_standbys
"""

DB_SQL = """
SELECT COALESCE(sum(xact_commit), 0)::float8 AS commits, COALESCE(sum(xact_rollback), 0)::float8 AS rollbacks,
       COALESCE(sum(deadlocks), 0)::float8 AS deadlocks
FROM pg_stat_database
"""

WAITS_SQL = "SELECT count(*)::float8 AS waiting FROM pg_locks WHERE NOT granted"


class Collector:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.Lock()

    def connect(self):
        return psycopg2.connect(
            host=self.cfg.get("host", "/var/run/postgresql"),
            port=int(self.cfg.get("db_port", 5432)),
            user=self.cfg["user"],
            password=self.cfg.get("password"),
            dbname=self.cfg.get("dbname", "postgres"),
            connect_timeout=3,
            options="-c statement_timeout=5000",
            sslmode="prefer" if not str(self.cfg.get("host", "/")).startswith("/") else "disable",
        )

    def collect(self, reg: promlib.Registry):
        t0 = time.monotonic()
        try:
            conn = self.connect()
        except Exception:
            reg.gauge("ha_lab_db_up", 0, help_="1 when the collector can query the database.")
            reg.gauge("ha_lab_writable", 0)
            return
        try:
            conn.autocommit = True
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
                self._collect(c, reg)
        except psycopg2.Error:
            reg.gauge("ha_lab_db_up", 0, help_="1 when the collector can query the database.")
            reg.gauge("ha_lab_writable", 0)
            return
        finally:
            conn.close()
        reg.gauge("ha_lab_db_up", 1, help_="1 when the collector can query the database.")
        reg.gauge("ha_lab_collector_duration_seconds", time.monotonic() - t0)

    def _q(self, c, sql):
        try:
            c.execute(sql)
            return c.fetchall()
        except psycopg2.Error:
            return []

    def _collect(self, c, reg):
        c.execute(STATE_SQL)
        s = c.fetchone()
        in_recovery, fenced = bool(s["in_recovery"]), bool(s["fenced"])
        read_only = in_recovery or fenced
        reg.gauge("ha_lab_read_only", 1 if read_only else 0)
        reg.gauge("ha_lab_writable", 0 if read_only else 1,
                  help_="1 when this node accepts writes (not in recovery, not fenced).")
        reg.gauge("ha_lab_replication_position", s["position"],
                  help_="Replication position: WAL byte offset (replay LSN on a standby, insert LSN on a primary). "
                        "Equal values across nodes indicate convergence.")
        reg.gauge("ha_lab_postgres_timeline", s["timeline"])
        reg.gauge("ha_lab_replica_configured", 1 if in_recovery else 0)
        if in_recovery:
            reg.gauge("ha_lab_replica_io_running", 1 if s["receiver_status"] == "streaming" else 0)
            reg.gauge("ha_lab_replica_sql_running", 0 if s["paused"] else 1)
            received = s["received"] if s["received"] is not None else s["replayed"]
            backlog = max((received or 0) - (s["replayed"] or 0), 0)
            reg.gauge("replication_queue_size", backlog, {"queue": "wal_replay"},
                      help_="WAL received and not yet replayed, in bytes.")
            lag = 0.0 if backlog == 0 else max(float(s["replay_age"] or 0), 0.0)
            reg.gauge("replication_lag_seconds", lag,
                      help_="Now minus the commit time of the last replayed transaction; 0 when nothing is queued.")
        else:
            reg.gauge("ha_lab_streaming_standbys", s["streaming"])
            reg.gauge("ha_lab_sync_standbys", s["sync_standbys"],
                      help_="Standbys whose commits this primary waits for (sync or quorum).")

        d = self._q(c, DB_SQL)
        if d:
            reg.counter("database_transactions_total", d[0]["commits"] + d[0]["rollbacks"])
            reg.counter("database_deadlocks_total", d[0]["deadlocks"])
        w = self._q(c, WAITS_SQL)
        if w:
            reg.gauge("database_lock_waits_current", w[0]["waiting"])
