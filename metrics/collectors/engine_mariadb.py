#!/usr/bin/env python3
"""MariaDB engine collector: server-side latency histogram, replication and HA state.

Loaded by ha_lab_collector.py when the config says engine=mariadb. Same contract as every engine module:
Collector(cfg) with .lock and .collect(registry), emitting at least ha_lab_db_up, ha_lab_writable,
ha_lab_read_only and ha_lab_replication_position.

MariaDB differs from MySQL here:
- GTIDs are domain-server-seq; the position is the sum of the highest seq_no per domain in gtid_current_pos.
- No super_read_only and no Performance Schema replication tables: replica state comes from SHOW SLAVE STATUS.
- Server-side statement time comes from the query_response_time plugin (information_schema.QUERY_RESPONSE_TIME),
  which is enabled by the lab's server config. There is no per-COMMIT or per-transaction histogram.

Config: /etc/ha-lab/collector.json
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import promlib  # noqa: E402

import pymysql  # noqa: E402


def gtid_map(pos: str) -> dict[str, int]:
    """'0-1-100,3-2-7' -> {'0': 100, '3': 7}."""
    out: dict[str, int] = {}
    for member in (pos or "").replace("\n", "").split(","):
        parts = member.strip().split("-")
        if len(parts) == 3 and parts[2].isdigit():
            out[parts[0]] = int(parts[2])
    return out


def gtid_count(pos: str) -> int:
    return sum(gtid_map(pos).values())


def gtid_behind(received: str, applied: str) -> int:
    done = gtid_map(applied)
    return sum(max(0, seq - done.get(domain, 0)) for domain, seq in gtid_map(received).items())


class Collector:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.buckets = tuple(cfg.get("buckets") or promlib.DEFAULT_BUCKETS)
        self.lock = threading.Lock()

    def connect(self):
        where = ({"host": self.cfg["host"], "port": int(self.cfg.get("mysql_port", 3306))} if self.cfg.get("host")
                 else {"unix_socket": self.cfg.get("socket", "/run/mysqld/mysqld.sock")})
        return pymysql.connect(
            user=self.cfg["user"],
            password=self.cfg["password"],
            **where,
            connect_timeout=3,
            read_timeout=5,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
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
            with conn.cursor() as c:
                self._collect(c, reg)
        finally:
            conn.close()
        reg.gauge("ha_lab_db_up", 1, help_="1 when the collector can query the database.")
        reg.gauge("ha_lab_collector_duration_seconds", time.monotonic() - t0)

    def _q(self, c, sql, args=None):
        try:
            c.execute(sql, args)
            return c.fetchall()
        except pymysql.MySQLError:
            return []

    def _collect(self, c, reg):
        v = self._q(c, "SELECT @@GLOBAL.read_only ro, @@GLOBAL.gtid_current_pos g, @@GLOBAL.gtid_slave_pos sp, "
                       "@@GLOBAL.wsrep_on wsrep")[0]
        status = {r["VARIABLE_NAME"].lower(): r["VARIABLE_VALUE"] for r in self._q(
            c, "SELECT VARIABLE_NAME, VARIABLE_VALUE FROM information_schema.GLOBAL_STATUS WHERE VARIABLE_NAME IN "
               "('wsrep_ready','wsrep_cluster_size','wsrep_cluster_status','wsrep_local_recv_queue',"
               "'wsrep_local_send_queue','Rpl_semi_sync_master_status','Rpl_semi_sync_master_clients',"
               "'Rpl_semi_sync_slave_status','Innodb_row_lock_time','Innodb_row_lock_waits',"
               "'Innodb_row_lock_current_waits','Innodb_deadlocks')")}

        ro = int(v["ro"])
        reg.gauge("ha_lab_read_only", ro)
        reg.gauge("ha_lab_replication_position", gtid_count(v["g"]),
                  help_="Replication position: sum of the highest seq_no per domain in gtid_current_pos (MariaDB). "
                        "Equal values across nodes indicate convergence.")
        writable = ro == 0
        if int(v["wsrep"]) == 1:
            ready = status["wsrep_ready"] == "ON"
            primary = status.get("wsrep_cluster_status") == "Primary"
            writable = writable and ready and primary
            reg.gauge("ha_lab_wsrep_ready", 1 if ready else 0)
            reg.gauge("ha_lab_wsrep_primary_component", 1 if primary else 0)
            reg.gauge("ha_lab_wsrep_cluster_size", status.get("wsrep_cluster_size"))
            reg.gauge("replication_queue_size", status.get("wsrep_local_recv_queue"), {"queue": "wsrep_recv"})
            reg.gauge("replication_queue_size", status.get("wsrep_local_send_queue"), {"queue": "wsrep_send"})
        if "rpl_semi_sync_master_status" in status:
            reg.gauge("ha_lab_semi_sync_source_active", 1 if status["rpl_semi_sync_master_status"] == "ON" else 0)
            reg.gauge("ha_lab_semi_sync_replicas", status.get("rpl_semi_sync_master_clients"),
                      help_="Semi-sync replicas currently connected to this node.")
        reg.gauge("ha_lab_writable", 1 if writable else 0,
                  help_="1 when this node accepts writes (read_only off, wsrep ready).")

        self._replica(c, reg, v["sp"])
        self._latency(c, reg)
        self._locks(reg, status)

    def _replica(self, c, reg, applied):
        rows = self._q(c, "SHOW SLAVE STATUS")
        reg.gauge("ha_lab_replica_configured", 1 if rows else 0)
        if not rows:
            return
        r = rows[0]
        reg.gauge("ha_lab_replica_io_running", 1 if r.get("Slave_IO_Running") == "Yes" else 0)
        reg.gauge("ha_lab_replica_sql_running", 1 if r.get("Slave_SQL_Running") == "Yes" else 0)
        reg.gauge("replication_last_error_number", max(int(r.get("Last_IO_Errno") or 0), int(r.get("Last_SQL_Errno") or 0)))
        reg.gauge("replication_queue_size", gtid_behind(r.get("Gtid_IO_Pos") or "", applied or ""), {"queue": "relay_log"})
        if r.get("Seconds_Behind_Master") is not None:
            reg.gauge("replication_lag_seconds", r["Seconds_Behind_Master"],
                      help_="Seconds_Behind_Master: replica clock minus the original commit time of the event "
                            "being applied. Crosses clocks.")

    def _latency(self, c, reg):
        rows = self._q(c, "SELECT TIME, COUNT, TOTAL FROM information_schema.QUERY_RESPONSE_TIME")
        if not rows:
            return
        bounds, total_count, total_sum = [], 0, 0.0
        for r in rows:
            total_count += int(r["COUNT"])
            try:
                total_sum += float(r["TOTAL"] or 0)
                bounds.append((float(r["TIME"]), int(r["COUNT"])))
            except ValueError:
                pass  # "TOO LONG" overflow bucket: counted in the total only
        bounds.sort()
        cum, i, seen = [], 0, 0
        for le in self.buckets:
            while i < len(bounds) and bounds[i][0] <= le:
                seen += bounds[i][1]
                i += 1
            cum.append((le, seen))
        cum.append((math.inf, total_count))
        reg.histogram("database_statement_duration_seconds", cum, total_sum, {"side": "server"},
                      "Server-side statement execution time (query_response_time plugin, all statements).")

    def _locks(self, reg, status):
        if "innodb_row_lock_time" in status:
            reg.counter("database_lock_wait_seconds_total", float(status["innodb_row_lock_time"]) / 1000,
                        help_="InnoDB row lock wait time (Innodb_row_lock_time, ms resolution).")
            reg.counter("database_lock_waits_total", status.get("innodb_row_lock_waits"))
            reg.gauge("database_lock_waits_current", status.get("innodb_row_lock_current_waits"))
        if "innodb_deadlocks" in status:
            reg.counter("database_deadlocks_total", status["innodb_deadlocks"])
