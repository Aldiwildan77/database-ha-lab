#!/usr/bin/env python3
"""Compares runs grouped by engine, HA provider and test: median with min-max over repeats.

  compare.py --suite benchmark/results/suites/<id>
  compare.py --runs run-20261126-002 run-20261126-003 [--out <dir>]
"""

import argparse
import json
import os
import statistics

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESULTS = os.path.join(ROOT, "benchmark", "results")

# (results.json key, column, scale, decimals, worst-case aggregate instead of median)
METRICS = [
    ("tps", "TPS", 1, 1, None),
    ("error_rate", "Errors %", 100, 2, None),
    ("latency_p50_s", "p50 ms", 1000, 0, None),
    ("latency_p99_s", "p99 ms", 1000, 0, None),
    ("commit_p99_s", "Commit p99 ms", 1000, 0, None),
    ("rto_s", "RTO s", 1, 1, None),
    ("write_availability", "Write avail %", 100, 1, None),
    ("lost_acknowledged_writes", "Lost (max)", 1, 0, max),
    ("consistency_errors", "Consistency errors (max)", 1, 0, max),
]


def load(run_id):
    d = os.path.join(RESULTS, run_id)
    try:
        r = json.load(open(os.path.join(d, "results.json")))
    except (OSError, ValueError):
        return None
    meta = json.load(open(os.path.join(d, "metadata.json")))
    ctx = json.load(open(os.path.join(d, "context.json")))
    r.setdefault("write_availability", (r.get("summary") or {}).get("write_availability"))
    r["_engine"] = f"{ctx.get('db_engine', 'mysql')} {ctx.get('db_version_config', '')}".strip()
    r["_fault"] = meta.get("fault") or "none"
    return r


def fmt(vals, scale, dec, agg):
    vals = [v * scale for v in vals if v is not None]
    if not vals:
        return "-"
    if agg:
        return f"{agg(vals):.{dec}f}"
    med = f"{statistics.median(vals):.{dec}f}"
    return med if len(vals) == 1 else f"{med} ({min(vals):.{dec}f}-{max(vals):.{dec}f})"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suite")
    ap.add_argument("--runs", nargs="*", default=[])
    ap.add_argument("--out")
    a = ap.parse_args()

    labels = {}
    if a.suite:
        s = json.load(open(os.path.join(a.suite, "suite.json")))
        labels = {r["run_id"]: r["label"] for r in s["runs"] if r["run_id"]}
        a.runs = list(labels) + a.runs
    if not a.runs:
        raise SystemExit("give --suite or --runs")
    out = a.out or a.suite or os.path.join(RESULTS, "compare")
    os.makedirs(out, exist_ok=True)

    groups, missing = {}, []
    for rid in a.runs:
        r = load(rid)
        if not r:
            missing.append(rid)
            continue
        test = labels.get(rid) or "_".join(x for x in (r["scenario"], r["_fault"]) if x != "none")
        groups.setdefault((r["_engine"], r["ha_provider"], test), []).append(r)

    head = ["Engine", "HA provider", "Test", "Runs"] + [m[1] for m in METRICS]
    lines = ["# Run comparison", "", "Median over repeats, (min-max) when repeated. Lost and consistency errors show the worst run.", "",
             "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    rows = []
    for (eng, ha, test), rs in sorted(groups.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][1])):
        cells = [eng, ha, test, str(len(rs))] + [fmt([r.get(k) for r in rs], sc, dc, ag) for k, _, sc, dc, ag in METRICS]
        lines.append("| " + " | ".join(cells) + " |")
        rows.append({"engine": eng, "ha_provider": ha, "test": test, "runs": [r["run_id"] for r in rs],
                     **{col: cells[4 + i] for i, (_, col, *_rest) in enumerate(METRICS)}})
    lines += ["", "Runs: " + ", ".join(a.runs)]
    if missing:
        lines.append("Without results (failed or interrupted): " + ", ".join(missing))

    open(os.path.join(out, "compare.md"), "w").write("\n".join(lines) + "\n")
    json.dump(rows, open(os.path.join(out, "compare.json"), "w"), indent=2)
    print("\n".join(lines))
    print(f"\n-> {os.path.relpath(os.path.join(out, 'compare.md'), ROOT)}")


if __name__ == "__main__":
    main()
