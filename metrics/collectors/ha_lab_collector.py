#!/usr/bin/env python3
"""ha-lab node collector: server-side latency histograms, replication and HA state.

Exposes metrics that mysqld_exporter does not provide in histogram form:
- database_statement_duration_seconds{side="server"}  (performance_schema.events_statements_histogram_global)
- commit_duration_seconds{side="server"}              (events_statements_histogram_by_digest for COMMIT)
- replication_apply_duration_seconds                  (sampled from replication_applier_status_by_worker)
- replication_lag_seconds / replication_queue_size
- ha_lab_* topology state used for failover detection

Performance Schema timers are picoseconds. Config: /etc/ha-lab/collector.json
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import promlib  # noqa: E402

import pymysql  # noqa: E402

PS = 1e12
CONFIG = os.environ.get("HA_LAB_COLLECTOR_CONFIG", "/etc/ha-lab/collector.json")


def gtid_count(gtid_set: str) -> int:
    total = 0
    for member in (gtid_set or "").replace("\n", "").split(","):
        for part in member.strip().split(":")[1:]:
            if not part or not part[0].isdigit():
                continue
            lo, _, hi = part.partition("-")
            total += (int(hi) if hi else int(lo)) - int(lo) + 1
    return total


class Collector:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.buckets = tuple(cfg.get("buckets") or promlib.DEFAULT_BUCKETS)
        self.apply_hist = promlib.Histogram(
            "replication_apply_duration_seconds",
            "Applier time of the last transaction per worker, sampled each scrape (START_APPLY to END_APPLY).",
            self.buckets,
        )
        self.delay_hist = promlib.Histogram(
            "replication_end_to_end_delay_seconds",
            "Source original commit to replica apply end, sampled. Crosses clocks: uncertain by the clock offset.",
            self.buckets,
        )
        self.last_applied: dict[tuple, str] = {}
        self.lock = threading.Lock()
        self.commit_digest = None

    def connect(self):
        where = ({"host": self.cfg["host"], "port": int(self.cfg.get("mysql_port", 3306))} if self.cfg.get("host")
                 else {"unix_socket": self.cfg.get("socket", "/var/run/mysqld/mysqld.sock")})
        return pymysql.connect(
            user=self.cfg["user"],
            password=self.cfg["password"],
            **where,
            connect_timeout=3,
            read_timeout=5,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )

    def ps_histogram(self, rows, reg, name, labels, total_sum, help_):
        """Map P_S histogram rows (BUCKET_TIMER_HIGH, COUNT_BUCKET_AND_LOWER) onto fixed buckets."""
        rows = sorted(rows, key=lambda r: int(r["BUCKET_TIMER_HIGH"]))
        cum, i, last = [], 0, 0
        for le in self.buckets:
            limit = le * PS
            while i < len(rows) and int(rows[i]["BUCKET_TIMER_HIGH"]) <= limit:
                last = int(rows[i]["COUNT_BUCKET_AND_LOWER"])
                i += 1
            cum.append((le, last))
        total = int(rows[-1]["COUNT_BUCKET_AND_LOWER"]) if rows else 0
        cum.append((math.inf, total))
        reg.histogram(name, cum, total_sum, labels, help_)

    def collect(self, reg: promlib.Registry):
        t0 = time.monotonic()
        try:
            conn = self.connect()
        except Exception:
            reg.gauge("ha_lab_mysql_up", 0, help_="1 when the collector can query MySQL.")
            reg.gauge("ha_lab_writable", 0)
            return
        try:
            with conn.cursor() as c:
                self._collect(c, reg)
        finally:
            conn.close()
        reg.gauge("ha_lab_mysql_up", 1, help_="1 when the collector can query MySQL.")
        reg.gauge("ha_lab_collector_duration_seconds", time.monotonic() - t0)

    def _q(self, c, sql, args=None):
        try:
            c.execute(sql, args)
            return c.fetchall()
        except pymysql.MySQLError:
            return []

    def _collect(self, c, reg):
        v = self._q(c, "SELECT @@read_only ro, @@super_read_only sro, @@gtid_executed g, "
                       "@@performance_schema ps")[0]
        status = {r["VARIABLE_NAME"].lower(): r["VARIABLE_VALUE"] for r in self._q(
            c, "SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME IN "
               "('wsrep_ready','wsrep_cluster_size','wsrep_cluster_status','wsrep_local_recv_queue',"
               "'wsrep_local_send_queue','wsrep_flow_control_paused','wsrep_local_state',"
               "'Rpl_semi_sync_source_status','Innodb_row_lock_time','Innodb_row_lock_waits',"
               "'Innodb_row_lock_current_waits')")}

        ro, sro = int(v["ro"]), int(v["sro"])
        reg.gauge("ha_lab_read_only", ro)
        reg.gauge("ha_lab_super_read_only", sro)
        reg.gauge("ha_lab_gtid_executed_transactions", gtid_count(v["g"]),
                  help_="Number of transactions in gtid_executed. Equal values across nodes indicate convergence.")

        members = self._q(c, "SELECT MEMBER_ID, MEMBER_HOST, MEMBER_STATE, MEMBER_ROLE, "
                             "(MEMBER_ID = @@server_uuid) AS self FROM performance_schema.replication_group_members "
                             "WHERE MEMBER_ID <> ''")
        me = next((m for m in members if int(m["self"]) == 1), None)
        writable = ro == 0 and sro == 0
        if me:
            writable = writable and me["MEMBER_STATE"] == "ONLINE" and me["MEMBER_ROLE"] == "PRIMARY"
            reg.gauge("ha_lab_group_member_online", 1 if me["MEMBER_STATE"] == "ONLINE" else 0)
            reg.gauge("ha_lab_group_primary", 1 if me["MEMBER_ROLE"] == "PRIMARY" else 0)
            for state in ("ONLINE", "RECOVERING", "OFFLINE", "ERROR", "UNREACHABLE"):
                reg.gauge("ha_lab_group_members", sum(1 for m in members if m["MEMBER_STATE"] == state),
                          {"state": state.lower()})
            stats = self._q(c, "SELECT COUNT_TRANSACTIONS_IN_QUEUE q, COUNT_TRANSACTIONS_REMOTE_IN_APPLIER_QUEUE a, "
                               "COUNT_CONFLICTS_DETECTED k FROM performance_schema.replication_group_member_stats "
                               "WHERE MEMBER_ID = @@server_uuid")
            if stats:
                reg.gauge("replication_queue_size", stats[0]["q"], {"queue": "certification"})
                reg.gauge("replication_queue_size", stats[0]["a"], {"queue": "applier"})
                reg.counter("replication_certification_conflicts_total", stats[0]["k"])
        if "wsrep_ready" in status:
            ready = status["wsrep_ready"] == "ON"
            primary = status.get("wsrep_cluster_status") == "Primary"
            writable = writable and ready and primary
            reg.gauge("ha_lab_wsrep_ready", 1 if ready else 0)
            reg.gauge("ha_lab_wsrep_primary_component", 1 if primary else 0)
            reg.gauge("ha_lab_wsrep_cluster_size", status.get("wsrep_cluster_size"))
            reg.gauge("replication_queue_size", status.get("wsrep_local_recv_queue"), {"queue": "wsrep_recv"})
            reg.gauge("replication_queue_size", status.get("wsrep_local_send_queue"), {"queue": "wsrep_send"})
        if "rpl_semi_sync_source_status" in status:
            reg.gauge("ha_lab_semi_sync_source_active", 1 if status["rpl_semi_sync_source_status"] == "ON" else 0)
        reg.gauge("ha_lab_writable", 1 if writable else 0,
                  help_="1 when this node accepts writes (read_only off, GR PRIMARY, wsrep ready).")

        self._replica(c, reg, v["g"])
        if int(v["ps"]) == 1:
            self._latency(c, reg)
        self._locks_storage(c, reg, status)

    def _replica(self, c, reg, executed):
        rows = self._q(c, "SELECT s.SERVICE_STATE io, a.SERVICE_STATE sqlt, s.RECEIVED_TRANSACTION_SET rcv, "
                          "s.LAST_ERROR_NUMBER io_err, (SELECT MAX(w.LAST_ERROR_NUMBER) FROM "
                          "performance_schema.replication_applier_status_by_worker w WHERE w.CHANNEL_NAME = '') sql_err "
                          "FROM performance_schema.replication_connection_status s "
                          "JOIN performance_schema.replication_applier_status a USING (CHANNEL_NAME) "
                          "WHERE CHANNEL_NAME = ''")
        reg.gauge("ha_lab_replica_configured", 1 if rows else 0)
        if rows:
            r = rows[0]
            reg.gauge("ha_lab_replica_io_running", 1 if r["io"] == "ON" else 0)
            reg.gauge("ha_lab_replica_sql_running", 1 if r["sqlt"] == "ON" else 0)
            reg.gauge("replication_last_error_number", max(int(r["io_err"] or 0), int(r["sql_err"] or 0)))
            pending = self._q(c, "SELECT GTID_SUBTRACT(%s, @@gtid_executed) p", (r["rcv"] or "",))
            reg.gauge("replication_queue_size", gtid_count(pending[0]["p"] if pending else ""), {"queue": "relay_log"})

        workers = self._q(
            c,
            "SELECT CHANNEL_NAME ch, WORKER_ID w, LAST_APPLIED_TRANSACTION t, "
            "TIMESTAMPDIFF(MICROSECOND, LAST_APPLIED_TRANSACTION_START_APPLY_TIMESTAMP, "
            "  LAST_APPLIED_TRANSACTION_END_APPLY_TIMESTAMP) apply_us, "
            "TIMESTAMPDIFF(MICROSECOND, LAST_APPLIED_TRANSACTION_ORIGINAL_COMMIT_TIMESTAMP, "
            "  LAST_APPLIED_TRANSACTION_END_APPLY_TIMESTAMP) e2e_us, "
            "APPLYING_TRANSACTION a, TIMESTAMPDIFF(MICROSECOND, APPLYING_TRANSACTION_ORIGINAL_COMMIT_TIMESTAMP, "
            "  NOW(6)) applying_lag_us "
            "FROM performance_schema.replication_applier_status_by_worker",
        )
        lag = 0.0
        for w in workers:
            key = (w["ch"], w["w"])
            if w["t"] and self.last_applied.get(key) != w["t"]:
                self.last_applied[key] = w["t"]
                channel = "group_replication" if w["ch"].startswith("group_replication") else "async"
                if w["apply_us"] is not None and w["apply_us"] >= 0:
                    self.apply_hist.observe(w["apply_us"] / 1e6, channel=channel)
                if w["e2e_us"] is not None and w["e2e_us"] >= 0:
                    self.delay_hist.observe(w["e2e_us"] / 1e6, channel=channel)
            if w["a"] and w["applying_lag_us"] is not None:
                lag = max(lag, w["applying_lag_us"] / 1e6)
        if workers:
            reg.gauge("replication_lag_seconds", lag,
                      help_="Now minus original commit time of the oldest transaction being applied (0 when idle). "
                            "Crosses clocks.")
        self.apply_hist.export(reg)
        self.delay_hist.export(reg)

    def _latency(self, c, reg):
        rows = self._q(c, "SELECT BUCKET_TIMER_HIGH, COUNT_BUCKET_AND_LOWER "
                          "FROM performance_schema.events_statements_histogram_global")
        s = self._q(c, "SELECT SUM(SUM_TIMER_WAIT) s FROM performance_schema.events_statements_summary_global_by_event_name")
        if rows:
            self.ps_histogram(rows, reg, "database_statement_duration_seconds", {"side": "server"},
                              float(s[0]["s"] or 0) / PS if s else 0,
                              "Server-side statement execution time (all statements).")
        if self.commit_digest is None:
            d = self._q(c, "SELECT STATEMENT_DIGEST('COMMIT') d")
            self.commit_digest = d[0]["d"] if d else ""
        if self.commit_digest:
            rows = self._q(c, "SELECT BUCKET_TIMER_HIGH, SUM(COUNT_BUCKET_AND_LOWER) COUNT_BUCKET_AND_LOWER "
                              "FROM performance_schema.events_statements_histogram_by_digest WHERE DIGEST = %s "
                              "GROUP BY BUCKET_NUMBER, BUCKET_TIMER_HIGH", (self.commit_digest,))
            s = self._q(c, "SELECT SUM(SUM_TIMER_WAIT) s FROM performance_schema.events_statements_summary_by_digest "
                           "WHERE DIGEST = %s", (self.commit_digest,))
            if rows:
                self.ps_histogram(rows, reg, "commit_duration_seconds", {"side": "server"},
                                  float(s[0]["s"] or 0) / PS if s else 0,
                                  "Server-side time of explicit COMMIT statements (includes binlog/redo flush "
                                  "and semi-sync/GR/Galera waits).")
        t = self._q(c, "SELECT SUM(COUNT_STAR) n, SUM(SUM_TIMER_WAIT) s "
                       "FROM performance_schema.events_transactions_summary_global_by_event_name")
        if t and t[0]["n"] is not None:
            reg.counter("database_transactions_total", t[0]["n"])
            reg.counter("database_transaction_seconds_total", float(t[0]["s"] or 0) / PS,
                        help_="Server-side transaction time (BEGIN..COMMIT). Mean only; no histogram source.")

    def _locks_storage(self, c, reg, status):
        if "innodb_row_lock_time" in status:
            reg.counter("database_lock_wait_seconds_total", float(status["innodb_row_lock_time"]) / 1000,
                        help_="InnoDB row lock wait time (Innodb_row_lock_time, ms resolution).")
            reg.counter("database_lock_waits_total", status.get("innodb_row_lock_waits"))
            reg.gauge("database_lock_waits_current", status.get("innodb_row_lock_current_waits"))
        d = self._q(c, "SELECT COUNT FROM information_schema.INNODB_METRICS WHERE NAME = 'lock_deadlocks'")
        if d:
            reg.counter("database_deadlocks_total", d[0]["COUNT"])
        files = self._q(c, "SELECT EVENT_NAME e, COUNT_MISC n, SUM_TIMER_MISC s, COUNT_WRITE wn, SUM_TIMER_WRITE ws, "
                           "COUNT_READ rn, SUM_TIMER_READ rs FROM performance_schema.file_summary_by_event_name "
                           "WHERE EVENT_NAME IN ('wait/io/file/innodb/innodb_log_file', "
                           "'wait/io/file/innodb/innodb_data_file', 'wait/io/file/sql/binlog')")
        names = {"wait/io/file/innodb/innodb_log_file": "redo", "wait/io/file/innodb/innodb_data_file": "data",
                 "wait/io/file/sql/binlog": "binlog"}
        for f in files:
            lbl = {"file": names[f["e"]]}
            reg.counter("storage_fsync_total", f["n"], lbl, "File misc operations (fsync and similar) per file class.")
            reg.counter("storage_fsync_seconds_total", float(f["s"]) / PS, lbl)
            reg.counter("storage_write_total", f["wn"], lbl)
            reg.counter("storage_write_seconds_total", float(f["ws"]) / PS, lbl)
            reg.counter("storage_read_total", f["rn"], lbl)
            reg.counter("storage_read_seconds_total", float(f["rs"]) / PS, lbl)


def main():
    with open(CONFIG) as fh:
        cfg = json.load(fh)
    col = Collector(cfg)

    def collect(reg):
        with col.lock:
            col.collect(reg)

    promlib.serve(int(cfg.get("port", 9106)), collect)


if __name__ == "__main__":
    main()
