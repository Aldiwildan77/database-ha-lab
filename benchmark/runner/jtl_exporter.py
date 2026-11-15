#!/usr/bin/env python3
"""Tails a JMeter JTL (CSV) and exposes client-side metrics for Prometheus (job="benchmark").

Level 0: ha_transaction_duration_seconds, ha_transaction_total, ha_transaction_success_total, ha_transaction_errors_total
Level 1/2 (client side): connection_duration_seconds, database_statement_duration_seconds{side="client"},
  commit_duration_seconds{side="client"}, rollback_duration_seconds, transaction_duration_seconds{side="client"},
  connection_errors_total, benchmark_active_threads
Durations come from System.nanoTime() inside the sampler (txn_ns etc.), not from JMeter's millisecond elapsed.
run_id is deliberately not a label; it lives in the JTL and run metadata.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "metrics", "collectors"))
import promlib  # noqa: E402

BUCKETS = (0.001, 0.0025, 0.005, 0.0075, 0.01, 0.015, 0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1, 2.5, 5, 10, 30)


class State:
    def __init__(self, base: dict, buckets):
        self.base = base
        H = lambda n, h: promlib.Histogram(n, h, buckets)  # noqa: E731
        self.txn = H("ha_transaction_duration_seconds", "End-to-end transaction time measured by the client (connect if needed + statements + commit).")
        self.conn = H("connection_duration_seconds", "Client connection establishment incl. TLS, auth and endpoint discovery.")
        self.stmt = H("database_statement_duration_seconds", "Client-observed statement time inside the transaction (network + server).")
        self.commit = H("commit_duration_seconds", "Client-observed COMMIT round trip.")
        self.rollback = H("rollback_duration_seconds", "Client-observed ROLLBACK after an error.")
        self.body = H("transaction_duration_seconds", "Client-observed BEGIN..COMMIT (statements + commit, excludes connect).")
        self.total = promlib.Counter("ha_transaction_total", "Transactions attempted.")
        self.ok = promlib.Counter("ha_transaction_success_total", "Transactions committed and acknowledged.")
        self.err = promlib.Counter("ha_transaction_errors_total", "Failed transactions by bounded error class.")
        self.conn_err = promlib.Counter("connection_errors_total", "Failed connection attempts from the client.")
        self.threads = 0
        self.last_ts = 0
        self.lock = threading.Lock()

    def add(self, row: dict):
        op = row.get("op") or row.get("label", "unknown")
        lbl = {**self.base, "operation": op}
        ns = lambda k: int(row.get(k) or 0) / 1e9  # noqa: E731
        ok = row.get("success") == "true"
        cls = row.get("err_class") or ""
        self.total.inc(**lbl)
        self.txn.observe(ns("txn_ns"), **lbl, outcome="success" if ok else "error")
        if int(row.get("connect_ns") or 0) > 0:
            self.conn.observe(ns("connect_ns"), **self.base)
        if ok:
            self.ok.inc(**lbl)
            self.stmt.observe(ns("exec_ns"), **lbl, side="client")
            self.commit.observe(ns("commit_ns"), **lbl, side="client")
            self.body.observe(ns("exec_ns") + ns("commit_ns"), **lbl, side="client")
        else:
            self.err.inc(**lbl, error_class=cls or "other")
            if cls in ("connection", "not_ready") and int(row.get("exec_ns") or 0) == 0:
                self.conn_err.inc(**self.base)
            if int(row.get("rollback_ns") or 0) > 0:
                self.rollback.observe(ns("rollback_ns"), **lbl)
        self.threads = int(row.get("allThreads") or 0)
        self.last_ts = int(row.get("timeStamp") or 0)

    def collect(self, reg):
        with self.lock:
            for m in (self.txn, self.conn, self.stmt, self.commit, self.rollback, self.body,
                      self.total, self.ok, self.err, self.conn_err):
                m.export(reg)
            reg.gauge("benchmark_active_threads", self.threads, self.base, "JMeter threads running (allThreads).")
            reg.gauge("benchmark_last_sample_timestamp_seconds", self.last_ts / 1000, self.base)


def tail(path, state: State, stop: threading.Event):
    while not os.path.exists(path) and not stop.is_set():
        time.sleep(0.2)
    with open(path, newline="") as fh:
        header = None
        buf = ""
        while True:
            chunk = fh.read()
            if chunk:
                buf += chunk
                lines = buf.split("\n")
                buf = lines.pop()  # incomplete last line
                text = "\n".join(lines)
                if header is None and lines:
                    header = next(csv.reader([lines[0]]))
                    text = "\n".join(lines[1:])
                if text:
                    with state.lock:
                        for rec in csv.reader(io.StringIO(text)):
                            if rec:
                                state.add(dict(zip(header, rec)))
            elif stop.is_set():
                return
            else:
                time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jtl", required=True)
    ap.add_argument("--port", type=int, default=9464)
    ap.add_argument("--label", action="append", default=[], help="k=v constant label (environment, ha_provider, scenario, endpoint_type)")
    a = ap.parse_args()
    base = dict(x.split("=", 1) for x in a.label)
    state = State(base, BUCKETS)
    stop = threading.Event()
    threading.Thread(target=tail, args=(a.jtl, state, stop), daemon=True).start()
    promlib.serve(a.port, state.collect)


if __name__ == "__main__":
    main()
