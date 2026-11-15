#!/usr/bin/env python3
"""Builds prometheus-summary.json, results.json and report.md for a run directory.

Usage: report.py <benchmark/results/run-id> [--prometheus http://127.0.0.1:9090]
Re-running is safe; it only reads the run's files (and Prometheus, if reachable).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "correlation"))
sys.path.insert(0, os.path.join(HERE, "..", "validation"))
import analyze  # noqa: E402
import consistency  # noqa: E402

PROM_QUERIES = {
    # name: (query template with {d} = range, unit)
    "cpu_utilization_avg": ('avg_over_time(lab:node_cpu_utilization:ratio{{job="node"}}[{d}])', "ratio"),
    "cpu_utilization_max": ('max_over_time(lab:node_cpu_utilization:ratio{{job="node"}}[{d}])', "ratio"),
    "memory_utilization_avg": ('avg_over_time(lab:node_memory_utilization:ratio{{job="node"}}[{d}])', "ratio"),
    "disk_iops_avg": ('sum by (node) (rate(node_disk_reads_completed_total{{device!~"loop.*|ram.*|sr.*|dm-.*"}}[{d}]) '
                      '+ rate(node_disk_writes_completed_total{{device!~"loop.*|ram.*|sr.*|dm-.*"}}[{d}]))', "iops"),
    "disk_write_latency_avg": ('sum by (node) (rate(node_disk_write_time_seconds_total{{device!~"loop.*|ram.*|sr.*|dm-.*"}}[{d}])) '
                               '/ sum by (node) (rate(node_disk_writes_completed_total{{device!~"loop.*|ram.*|sr.*|dm-.*"}}[{d}]))', "s"),
    "disk_read_latency_avg": ('sum by (node) (rate(node_disk_read_time_seconds_total{{device!~"loop.*|ram.*|sr.*|dm-.*"}}[{d}])) '
                              '/ sum by (node) (rate(node_disk_reads_completed_total{{device!~"loop.*|ram.*|sr.*|dm-.*"}}[{d}]))', "s"),
    "tcp_retransmits_per_s": ("rate(node_netstat_Tcp_RetransSegs[{d}])", "1/s"),
    "replication_lag_max": ("max_over_time(replication_lag_seconds[{d}])", "s"),
    "server_commit_p50": ('histogram_quantile(0.5, sum by (node, le) (increase(commit_duration_seconds_bucket{{side="server"}}[{d}])))', "s"),
    "server_commit_p99": ('histogram_quantile(0.99, sum by (node, le) (increase(commit_duration_seconds_bucket{{side="server"}}[{d}])))', "s"),
    "server_statement_p99": ('histogram_quantile(0.99, sum by (node, le) (increase(database_statement_duration_seconds_bucket{{side="server"}}[{d}])))', "s"),
    "replication_apply_p99": ("histogram_quantile(0.99, sum by (node, le) (increase(replication_apply_duration_seconds_bucket[{d}])))", "s"),
    "lock_wait_seconds": ("increase(database_lock_wait_seconds_total[{d}])", "s"),
    "deadlocks": ("increase(database_deadlocks_total[{d}])", "count"),
    "redo_fsync_avg": ('increase(storage_fsync_seconds_total{{file="redo"}}[{d}]) / increase(storage_fsync_total{{file="redo"}}[{d}])', "s"),
    "node_rtt_p50": ('histogram_quantile(0.5, sum by (node, peer, le) (increase(network_rtt_seconds_bucket{{job="node"}}[{d}])))', "s"),
    "client_rtt_p50": ('histogram_quantile(0.5, sum by (peer, le) (increase(network_rtt_seconds_bucket{{job="local_probe"}}[{d}])))', "s"),
}


def prom_query(base, query, at):
    url = f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": query, "time": f"{at:.3f}"})
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.load(r)
    out = []
    for s in data.get("data", {}).get("result", []):
        v = float(s["value"][1])
        out.append({"labels": {k: v for k, v in s["metric"].items() if k in ("node", "peer", "file")},
                    "value": None if v != v else v})
    return out


# Runs from before engine descriptors carried report queries (MySQL only).
LEGACY_ENGINE_QUERIES = {
    "seconds_behind_source_max": {"query": "max_over_time(mysql_slave_status_seconds_behind_source[{d}]) "
                                           "or max_over_time(mysql_slave_status_seconds_behind_master[{d}])", "unit": "s"},
}


def report_queries(ctx):
    """Engine-neutral queries plus the engine's own (engine descriptor: report_queries)."""
    engine = ctx.get("engine_report_queries")
    if engine is None:
        engine = LEGACY_ENGINE_QUERIES if ctx.get("db_engine", "mysql") == "mysql" else {}
    return {**PROM_QUERIES, **{k: (v["query"], v.get("unit", "")) for k, v in engine.items()}}


def prometheus_summary(base, start, end, queries):
    d = max(60, int(end - start))
    res = {"window": {"start": start, "end": end, "range_s": d}, "metrics": {}}
    for name, (q, unit) in queries.items():
        try:
            res["metrics"][name] = {"unit": unit, "series": prom_query(base, q.format(d=f"{d}s"), end)}
        except Exception as e:  # Prometheus down or query error: keep the report going
            res["metrics"][name] = {"unit": unit, "error": str(e)}
    return res


def fmt(v, unit="", digits=2):
    if v is None:
        return "n/a"
    if unit == "ms":
        return f"{v * 1000:.{digits}f} ms"
    if unit == "%":
        return f"{v * 100:.{digits}f} %"
    return f"{v:.{digits}f}{(' ' + unit) if unit else ''}"


def series_table(summary, name, unit):
    m = summary["metrics"].get(name, {})
    if "error" in m or not m.get("series"):
        return "n/a"
    parts = []
    for s in m["series"]:
        who = s["labels"].get("node") or s["labels"].get("peer") or "-"
        if "peer" in s["labels"] and "node" in s["labels"]:
            who = f"{s['labels']['node']}→{s['labels']['peer']}"
        v = s["value"]
        parts.append(f"{who}: " + (fmt(v, "ms") if unit == "s" else fmt(v, "%") if unit == "ratio" else fmt(v, unit, 1)))
    return "; ".join(parts)


def build(run_dir, prometheus):
    meta = json.load(open(os.path.join(run_dir, "metadata.json")))
    ctx = json.load(open(os.path.join(run_dir, "context.json")))
    rows = list(analyze.load_jtl(os.path.join(run_dir, "jmeter.jtl")))
    summary = analyze.summarize(rows, meta.get("warmup_s", 0))
    events = json.load(open(os.path.join(run_dir, "failover-events.json"))) if os.path.exists(
        os.path.join(run_dir, "failover-events.json")) else []
    network = json.load(open(os.path.join(run_dir, "network.json"))) if os.path.exists(
        os.path.join(run_dir, "network.json")) else {}

    ev = {e["event"]: e for e in events}
    failover = {}
    if "fault_injected" in ev:
        f = ev["fault_injected"]["ts"]
        failover = analyze.rto(rows, f * 1000)
        for name, key in (("detection_s", "primary_unavailable"), ("promotion_s", "new_primary_writable"),
                          ("recovery_s", "all_nodes_healthy"), ("replication_converged_s", "replication_converged")):
            failover[name] = (ev[key]["ts"] - f) if key in ev else None
        if "primary_unavailable" in ev and "new_primary_writable" in ev:
            failover["failover_duration_s"] = ev["new_primary_writable"]["ts"] - ev["primary_unavailable"]["ts"]
        failover["fault"] = ev["fault_injected"].get("fault")
        failover["node"] = ev["fault_injected"].get("node")

    cons_path = os.path.join(run_dir, "consistency.json")
    if os.path.isdir(os.path.join(run_dir, "ledger")):
        final = meta.get("final_primary") or (ctx.get("writable_nodes") or [None])[0]
        cons = consistency.compare(rows, os.path.join(run_dir, "ledger"), final)
        json.dump(cons, open(cons_path, "w"), indent=2)
    else:
        cons = json.load(open(cons_path)) if os.path.exists(cons_path) else {}

    prom = prometheus_summary(prometheus, meta["started_at_epoch"], meta["finished_at_epoch"], report_queries(ctx))
    json.dump(prom, open(os.path.join(run_dir, "prometheus-summary.json"), "w"), indent=2)

    results = {
        "run_id": meta["run_id"], "scenario": meta["scenario"], "ha_provider": ctx["ha_provider"],
        "environment": ctx["environment"], "endpoint_type": ctx["endpoint"]["type"],
        "client_location": ctx["client_location"], "started_at": meta["started_at"],
        "threads": meta["jmeter_properties"].get("threads"), "duration_s": summary.get("duration_s"),
        "tps": summary.get("tps"), "rps": summary.get("rps"), "error_rate": summary.get("error_rate"),
        "latency_p50_s": summary["latency_s"]["transaction"].get("p50") if summary.get("samples") else None,
        "latency_p95_s": summary["latency_s"]["transaction"].get("p95") if summary.get("samples") else None,
        "latency_p99_s": summary["latency_s"]["transaction"].get("p99") if summary.get("samples") else None,
        "latency_avg_s": summary["latency_s"]["transaction"].get("avg") if summary.get("samples") else None,
        "commit_p99_s": summary["latency_s"]["commit_writes"].get("p99") if summary.get("samples") else None,
        "write_availability": summary.get("write_availability"),
        "read_availability": summary.get("read_availability"),
        "rto_s": failover.get("rto_s"), "detection_s": failover.get("detection_s"),
        "failover_duration_s": failover.get("failover_duration_s"),
        "lost_acknowledged_writes": cons.get("lost_acknowledged_writes"),
        "consistency_errors": cons.get("consistency_errors"),
        "summary": summary, "failover": failover,
    }
    json.dump(results, open(os.path.join(run_dir, "results.json"), "w"), indent=2)
    with open(os.path.join(run_dir, "report.md"), "w") as fh:
        fh.write(render(meta, ctx, summary, failover, cons, prom, network, events))
    append_index(os.path.dirname(os.path.abspath(run_dir)), results)
    return results


def append_index(results_root, r):
    cols = ["run_id", "scenario", "ha_provider", "environment", "endpoint_type", "client_location", "started_at",
            "threads", "duration_s", "tps", "rps", "error_rate", "latency_avg_s", "latency_p50_s", "latency_p95_s",
            "latency_p99_s", "commit_p99_s", "write_availability", "read_availability", "rto_s", "detection_s",
            "failover_duration_s", "lost_acknowledged_writes", "consistency_errors"]
    path = os.path.join(results_root, "summary.csv")
    existing = []
    if os.path.exists(path):
        existing = [x for x in csv.DictReader(open(path)) if x["run_id"] != r["run_id"]]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for x in existing:
            w.writerow({k: x.get(k, "") for k in cols})
        w.writerow({k: r.get(k) for k in cols})


def render(meta, ctx, s, fo, cons, prom, net, events):
    L = []
    lat = s.get("latency_s", {})
    t = lat.get("transaction", {})
    L += [f"# Benchmark report — {meta['run_id']}", "",
          f"- **Scenario:** {meta['scenario']}",
          f"- **HA architecture:** {ctx['ha_provider']}",
          f"- **Endpoint:** {ctx['endpoint']['type']} ({ctx['endpoint']['target']}, mode `{ctx['endpoint']['mode']}`): "
          f"{', '.join(ctx['endpoint']['endpoints'])}",
          f"- **Client location:** {ctx['client_location']} (JMeter on the local machine)",
          f"- **Started:** {meta['started_at']} — **finished:** {meta['finished_at']}",
          f"- **Environment:** {ctx['environment']}", ""]
    if ctx["endpoint"]["type"] == "direct":
        L += ["> Direct-to-node run: measures one node, not client-facing HA behaviour. "
              "Do not compare directly with `ha` endpoint runs.", ""]

    L += ["## Topology", "", "| Node | Datacenter | Network | Configured role | Writable at start | Database | vCPU | RAM | OS | Clock offset | Synced |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for n in ctx["nodes"]:
        L.append(f"| {n['name']} | {n['datacenter']} | {n.get('network', '-')} ({n.get('network_type', '-')}) | {n['configured_role']} | {n['writable']} | {ctx.get('db_engine', 'mysql')} {n.get('db_version', n.get('mysql_version', '-'))} | "
                 f"{n['vcpus']} | {int(n['memory_mb']) // 1024} GiB | {n['os']} | {fmt(n['clock_offset_seconds'], 'ms', 3)} | {n['clock_synced']} |")
    L.append("")

    L += ["## Workload", "", "| Setting | Value |", "|---|---|"]
    for k in ("threads", "rampup", "duration", "read_ratio", "reads_per_txn", "rows", "seed", "think_ms",
              "spike_threads", "spike_at", "spike_duration"):
        if k in meta["jmeter_properties"]:
            L.append(f"| {k} | {meta['jmeter_properties'][k]} |")
    L += [f"| warm-up excluded | {s.get('warmup_excluded_s', 0)} s |", ""]

    L += ["## Results (client side, from JTL)", "",
          "| Metric | Value |", "|---|---|",
          f"| Samples | {s.get('samples')} over {fmt(s.get('duration_s'), 's', 1)} |",
          f"| TPS (committed) | {fmt(s.get('tps'), '', 1)} (writes {fmt(s.get('write_tps'), '', 1)}, reads {fmt(s.get('read_tps'), '', 1)}) |",
          f"| RPS (attempted) | {fmt(s.get('rps'), '', 1)} |",
          f"| Error rate | {fmt(s.get('error_rate'), '%')} |",
          f"| Errors by class | {s.get('errors_by_class') or '-'} |",
          f"| Write availability (requests / seconds) | {fmt(s.get('write_availability'), '%')} / {fmt(s.get('write_availability_time'), '%')} |",
          f"| Read availability (requests / seconds) | {fmt(s.get('read_availability'), '%')} / {fmt(s.get('read_availability_time'), '%')} |",
          f"| Peak threads | {s.get('max_threads')} |", ""]

    L += ["### Latency", "",
          "Measured with `System.nanoTime()` in the sampler. Components overlap and are **not additive**: "
          "transaction = connect (only when reconnecting) + statements + commit + client overhead.", "",
          "| Component | avg | p50 | p90 | p95 | p99 | max | n |", "|---|---|---|---|---|---|---|---|"]
    for name, key in (("Transaction (end-to-end)", "transaction"), ("Statements (client)", "statements"),
                      ("Commit (all, client)", "commit"), ("Commit (writes, client)", "commit_writes"),
                      ("Connection (reconnects)", "connection"), ("Failed transactions", "errors")):
        d = lat.get(key, {})
        if d.get("count"):
            L.append(f"| {name} | " + " | ".join(fmt(d.get(q), "ms") for q in ("avg", "p50", "p90", "p95", "p99", "max"))
                     + f" | {d['count']} |")
    for op, d in (lat.get("transaction_by_op") or {}).items():
        if d.get("count"):
            L.append(f"| Transaction ({op}) | " + " | ".join(fmt(d.get(q), "ms") for q in ("avg", "p50", "p90", "p95", "p99", "max"))
                     + f" | {d['count']} |")
    L.append("")

    L += ["## Server and host metrics (Prometheus)", "",
          f"Window: {dt.datetime.fromtimestamp(prom['window']['start'], dt.timezone.utc).isoformat()} + {prom['window']['range_s']} s", "",
          "| Metric | Per node |", "|---|---|"]
    for name, m in prom["metrics"].items():
        L.append(f"| {name} | {series_table(prom, name, m['unit'])} |")
    L.append("")

    if fo:
        L += ["## Failover", "",
              f"Fault `{fo.get('fault')}` on `{fo.get('node')}`. All event times are taken on the client clock.", "",
              "| Metric | Value |", "|---|---|",
              f"| Failure detection (fault → primary seen unavailable) | {fmt(fo.get('detection_s'), 's')} |",
              f"| Promotion (fault → another node writable) | {fmt(fo.get('promotion_s'), 's')} |",
              f"| Failover duration (detection → new primary writable) | {fmt(fo.get('failover_duration_s'), 's')} |",
              f"| **RTO** (fault → first successful write after the outage) | {fmt(fo.get('rto_s'), 's')} |",
              f"| Client-visible write outage | {fmt(fo.get('client_outage_s'), 's')} |",
              f"| Failed writes during outage | {fo.get('failed_writes_during_outage', 'n/a')} |",
              f"| Replication converged after fault | {fmt(fo.get('replication_converged_s'), 's')} |",
              f"| Recovery (all nodes healthy) | {fmt(fo.get('recovery_s'), 's')} |", ""]
        if fo.get("note"):
            L += [f"> {fo['note']}", ""]
        L += ["### Events", "", "| Time (UTC) | Event | Details |", "|---|---|---|"]
        for e in events:
            details = {k: v for k, v in e.items() if k not in ("event", "ts", "at")}
            L.append(f"| {e.get('at')} | {e['event']} | {details or ''} |")
        L.append("")

    if cons:
        L += ["## Consistency and RPO", "",
              f"- Acknowledged writes: {cons.get('acknowledged_writes')}; failed: {cons.get('failed_writes')}; "
              f"indeterminate (connection lost during COMMIT): {cons.get('indeterminate_writes')}",
              f"- Final primary: {cons.get('final_primary')}",
              f"- **RPO — acknowledged writes lost:** {cons.get('lost_acknowledged_writes', 'n/a')}",
              f"- Consistency errors (lost + phantom + duplicate): {cons.get('consistency_errors', 'n/a')}",
              f"- Diverged nodes: {cons.get('diverged_nodes', 'n/a')}; account checksum consistent: "
              f"{cons.get('accounts_checksum_consistent', 'n/a')}", "",
              "| Node | Reachable | Ledger rows | Missing acked | Present but failed | Indeterminate committed | Duplicates |",
              "|---|---|---|---|---|---|---|"]
        for n, e in (cons.get("nodes") or {}).items():
            L.append(f"| {n} | {e.get('reachable')} | {e.get('rows', '-')} | {e.get('missing_acknowledged', '-')} | "
                     f"{e.get('present_but_failed', '-')} | {e.get('indeterminate_committed', '-')} | {e.get('duplicates', '-')} |")
        L.append("")

    L += ["## Network", ""]
    if net.get("client_to_node"):
        L += ["| Client → node | RTT avg | RTT p50 | Loss | TCP connect | Path |", "|---|---|---|---|---|---|"]
        for n in net["client_to_node"]:
            L.append(f"| {n['node']} | {fmt(n.get('rtt_avg_s'), 'ms')} | {fmt(n.get('rtt_p50_s'), 'ms')} | "
                     f"{fmt(n.get('loss_ratio'), '%', 1)} | {fmt(n.get('tcp_connect_avg_s'), 'ms')} | {n.get('path') or n.get('tailscale_path')} |")
        L.append("")
    if net.get("pairs"):
        L += ["| Node → node | RTT min/avg/max | Loss | Path | Throughput |", "|---|---|---|---|---|"]
        for p in net["pairs"]:
            tp = f"{p['throughput_bps'] / 1e6:.0f} Mbit/s" if p.get("throughput_bps") else "-"
            L.append(f"| {p['src']} → {p['dst']} | {p.get('rtt_min_ms')}/{p.get('rtt_avg_ms')}/{p.get('rtt_max_ms')} ms | "
                     f"{p.get('loss_pct')} % | {p.get('path') or p.get('tailscale_path')} | {tp} |")
        L.append("")
    L.append(f"Measured at {net.get('measured_at', 'n/a')}.")
    L.append("")

    offsets = [abs(n["clock_offset_seconds"]) for n in ctx["nodes"] if n["clock_offset_seconds"] == n["clock_offset_seconds"]]
    L += ["## Measurement limitations", "",
          "- Client latencies are measured on the local machine and include the Tailscale path to the endpoint.",
          "- Server-side histograms (statement, commit) come from Performance Schema with ~10% bucket resolution, "
          "re-bucketed to fixed Prometheus buckets.",
          "- `replication_apply_duration_seconds` samples the last applied transaction per worker per scrape; "
          "it is not a complete distribution.",
          f"- Replication lag and end-to-end delay cross node clocks. Max measured clock offset at start: "
          f"{fmt(max(offsets) if offsets else None, 'ms', 3)}; treat cross-node differences below that as noise.",
          "- Failover events are observed by polling node collectors every 0.5 s from the client; "
          "detection and promotion times have ±0.5 s resolution plus scrape latency.",
          "- RPO counts acknowledged writes missing on the final primary. Writes whose COMMIT outcome is unknown "
          "(connection lost during commit) are reported separately, not as loss.",
          "- No result in this report implies zero data loss or zero downtime beyond what was measured in this run.", ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--prometheus", default=os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090"))
    a = ap.parse_args()
    r = build(a.run_dir, a.prometheus)
    print(json.dumps({k: v for k, v in r.items() if k not in ("summary", "failover")}, indent=2))


if __name__ == "__main__":
    main()
