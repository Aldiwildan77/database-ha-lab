"""JTL analysis: throughput, latency distribution, availability, RTO. Stdlib only.

Times: JMeter timeStamp is the sample start in client wall-clock ms; durations use the sampler's
monotonic nanosecond measurements. Fault/detection events come from the same client clock, so no
cross-host clock subtraction is involved.
"""

from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict

QUANTILES = (0.5, 0.9, 0.95, 0.99)


def load_jtl(path):
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            ts = int(row["timeStamp"])
            txn_ns = int(row.get("txn_ns") or 0) or int(row["elapsed"]) * 1_000_000
            yield {
                "start_ms": ts,
                "end_ms": ts + txn_ns / 1e6,
                "op": row.get("op") or row["label"],
                "ok": row["success"] == "true",
                "txn_id": row.get("txn_id", ""),
                "err": row.get("err_class", ""),
                "txn_s": txn_ns / 1e9,
                "connect_s": int(row.get("connect_ns") or 0) / 1e9,
                "exec_s": int(row.get("exec_ns") or 0) / 1e9,
                "commit_s": int(row.get("commit_ns") or 0) / 1e9,
                "served_by": row.get("served_by", ""),
                "threads": int(row.get("allThreads") or 0),
                "visible_ms": int(row["visible_ms"]) if (row.get("visible_ms") or "").lstrip("-").isdigit() else None,
            }


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    return s[max(0, min(len(s) - 1, math.ceil(q * len(s)) - 1))]


def dist(values):
    if not values:
        return {"count": 0}
    out = {"count": len(values), "avg": sum(values) / len(values), "min": min(values), "max": max(values)}
    for q in QUANTILES:
        out[f"p{int(q * 100)}"] = pct(values, q)
    return out


def summarize(rows, warmup_s=0.0):
    rows = sorted(rows, key=lambda r: r["start_ms"])
    if not rows:
        return {"samples": 0}
    t0 = rows[0]["start_ms"] + warmup_s * 1000
    window = [r for r in rows if r["start_ms"] >= t0] or rows
    span = max(1e-9, (max(r["end_ms"] for r in window) - window[0]["start_ms"]) / 1000)
    by_op = defaultdict(list)
    for r in window:
        by_op[r["op"]].append(r)
    reports = by_op.get("report", [])
    window = [r for r in window if r["op"] != "report"] or window   # reporting traffic is summarized on its own
    ok = [r for r in window if r["ok"]]
    writes = by_op.get("write", [])
    reads = by_op.get("read", [])

    def avail(rs):
        return (sum(1 for r in rs if r["ok"]) / len(rs)) if rs else None

    def time_avail(rs):
        attempted, succeeded = set(), set()
        for r in rs:
            sec = int(r["start_ms"] // 1000)
            attempted.add(sec)
            if r["ok"]:
                succeeded.add(int(r["end_ms"] // 1000))
        return (len(attempted & succeeded) / len(attempted)) if attempted else None

    return {
        "samples": len(window),
        "duration_s": span,
        "warmup_excluded_s": warmup_s,
        "rps": len(window) / span,
        "tps": len(ok) / span,
        "write_tps": sum(1 for r in writes if r["ok"]) / span,
        "read_tps": sum(1 for r in reads if r["ok"]) / span,
        "report_qps": sum(1 for r in reports if r["ok"]) / span,
        "error_rate": 1 - len(ok) / len(window),
        "errors_by_class": dict(Counter(r["err"] or "other" for r in window if not r["ok"])),
        "write_availability": avail(writes),
        "read_availability": avail(reads),
        "write_availability_time": time_avail(writes),
        "read_availability_time": time_avail(reads),
        "max_threads": max(r["threads"] for r in window),
        "latency_s": {
            "transaction": dist([r["txn_s"] for r in ok]),
            "transaction_by_op": {op: dist([r["txn_s"] for r in rs if r["ok"]]) for op, rs in by_op.items()},
            "connection": dist([r["connect_s"] for r in window if r["connect_s"] > 0]),
            "statements": dist([r["exec_s"] for r in ok]),
            "commit": dist([r["commit_s"] for r in ok]),
            "commit_writes": dist([r["commit_s"] for r in writes if r["ok"]]),
            "errors": dist([r["txn_s"] for r in window if not r["ok"]]),
        },
        "served_by": dict(Counter(r["served_by"] for r in ok)),
    }


def timeline(rows, bucket_s=1):
    out = defaultdict(lambda: {"write_ok": 0, "write_err": 0, "read_ok": 0, "read_err": 0})
    for r in rows:
        b = int(r["start_ms"] // (bucket_s * 1000)) * bucket_s
        out[b][f"{r['op']}_{'ok' if r['ok'] else 'err'}"] += 1
    return [{"t": k, **v} for k, v in sorted(out.items())]


def rto(rows, fault_ms):
    """RTO = fault -> first successful write that started after the first post-fault write failure.

    0 when no write failed after the fault (no client-visible outage)."""
    writes = sorted((r for r in rows if r["op"] == "write"), key=lambda r: r["start_ms"])
    first_fail = next((r for r in writes if r["start_ms"] >= fault_ms - 1 and not r["ok"]), None)
    if first_fail is None:
        return {"rto_s": 0.0, "first_failed_write_ms": None, "first_successful_write_ms": None,
                "note": "no failed write after the fault"}
    last_ok_before = max((r["end_ms"] for r in writes if r["ok"] and r["end_ms"] <= first_fail["start_ms"]), default=None)
    recovered = next((r for r in writes if r["ok"] and r["start_ms"] >= first_fail["start_ms"]), None)
    failed = sum(1 for r in writes if not r["ok"] and r["start_ms"] >= fault_ms - 1
                 and (recovered is None or r["start_ms"] < recovered["end_ms"]))
    return {
        "rto_s": ((recovered["end_ms"] - fault_ms) / 1000) if recovered else None,
        "client_outage_s": ((recovered["end_ms"] - last_ok_before) / 1000) if (recovered and last_ok_before) else None,
        "first_failed_write_ms": first_fail["start_ms"],
        "first_successful_write_ms": recovered["end_ms"] if recovered else None,
        "failed_writes_during_outage": failed,
        "note": "" if recovered else "writes never recovered before the run ended",
    }


def visibility(rows):
    """Stale-read check: delay until a committed write was readable on a replica (stale_check=1 runs)."""
    v = [r["visible_ms"] for r in rows if r.get("visible_ms") is not None]
    if not v:
        return {}
    seen = sorted(x for x in v if x >= 0)
    q = lambda p: seen[min(len(seen) - 1, int(len(seen) * p))] if seen else None  # noqa: E731
    return {"checked": len(v), "immediate": sum(1 for x in seen if x == 0), "not_visible": len(v) - len(seen),
            "p50_ms": q(0.5), "p99_ms": q(0.99), "max_ms": seen[-1] if seen else None}
