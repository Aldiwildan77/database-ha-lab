#!/usr/bin/env python3
"""Compares runs grouped by engine, HA provider and test: median with min-max over repeats.

  compare.py --suite benchmark/results/suites/<id>
  compare.py --runs run-20261126-002 run-20261126-003 [--out <dir>]
"""

import argparse
import json
import os
import statistics
from html import escape

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
    ("replica_visible_p99_ms", "Replica visible p99 ms", 1, 0, None),
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
    r["_settings"] = json.dumps(meta.get("ha_settings") or {}, sort_keys=True)
    return r


def fmt(vals, scale, dec, agg):
    vals = [v * scale for v in vals if v is not None]
    if not vals:
        return "-"
    if agg:
        return f"{agg(vals):.{dec}f}"
    med = f"{statistics.median(vals):.{dec}f}"
    return med if len(vals) == 1 else f"{med} ({min(vals):.{dec}f}-{max(vals):.{dec}f})"


CHARTS = [("tps", "TPS (higher is better)", 1), ("latency_p99_s", "p99 latency ms", 1000), ("rto_s", "RTO s", 1),
          ("write_availability", "Write availability %", 100), ("replica_visible_p99_ms", "Replica visible p99 ms", 1),
          ("lost_acknowledged_writes", "Lost acknowledged writes", 1)]


def bars(title, groups, key, scale):
    """Horizontal bars: median per group, whisker from min to max."""
    data = []
    for label, rs in groups:
        v = [r.get(key) * scale for r in rs if r.get(key) is not None]
        if v:
            data.append((label, statistics.median(v), min(v), max(v)))
    if not data:
        return ""
    w, lw, rh = 760, 300, 22
    top = max(d[3] for d in data) or 1
    h = 30 + rh * len(data)
    s = [f'<svg viewBox="0 0 {w} {h}" width="100%" font-family="Helvetica,Arial" font-size="11">',
         f'<text x="0" y="14" font-weight="bold" font-size="12">{escape(title)}</text>']
    for i, (label, med, lo, hi) in enumerate(data):
        y = 26 + i * rh
        x = lambda v: lw + (w - lw - 60) * v / top  # noqa: E731
        s += [f'<text x="{lw - 6}" y="{y + 12}" text-anchor="end">{escape(label[:60])}</text>',
              f'<rect x="{lw}" y="{y + 2}" width="{max(x(med) - lw, 1):.1f}" height="{rh - 8}" fill="#2b6cb0"/>',
              f'<line x1="{x(lo):.1f}" x2="{x(hi):.1f}" y1="{y + rh / 2 - 2}" y2="{y + rh / 2 - 2}" stroke="#1a202c"/>',
              f'<text x="{x(hi) + 4:.1f}" y="{y + 12}">{med:,.0f}</text>' if med >= 100 else
              f'<text x="{x(hi) + 4:.1f}" y="{y + 12}">{med:.3g}</text>']
    return "\n".join(s + ["</svg>"])


def html_report(path, title, table_md, groups, timelines):
    rows = [ln for ln in table_md if ln.startswith("|")]
    head = [c.strip() for c in rows[0].strip("|").split("|")]
    body = "".join("<tr>" + "".join(f"<td>{escape(c.strip())}</td>" for c in r.strip("|").split("|")) + "</tr>" for r in rows[2:])
    charts = "".join(f"<div class=c>{bars(t, groups, k, sc)}</div>" for k, t, sc in CHARTS)
    tl = "".join(f"<h3>{escape(label)}</h3><img src='{src}'>" for label, src in timelines)
    notes = "".join(f"<p>{escape(ln)}</p>" for ln in table_md if ln.startswith(("- S", "Runs:", "Without")))
    open(path, "w").write(f"""<!doctype html><meta charset=utf-8><title>{escape(title)}</title>
<style>body{{font:13px Helvetica,Arial;margin:24px;color:#1a202c;background:#fff}}table{{border-collapse:collapse;font-size:11px}}
td,th{{border:1px solid #e2e8f0;padding:3px 6px;text-align:left}}.c{{max-width:800px;margin:18px 0}}img{{max-width:100%}}</style>
<h1>{escape(title)}</h1><p>Median per group, whisker = min-max over repeats.</p>
<table><tr>{"".join(f"<th>{escape(h)}</th>" for h in head)}</tr>{body}</table>{notes}
<h2>Charts</h2>{charts}<h2>Timelines</h2>{tl}""")


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
        groups.setdefault((r["_engine"], r["ha_provider"], test, r["_settings"]), []).append(r)

    sets = sorted({k[3] for k in groups} - {"{}"})
    sid = {v: f"S{i + 1}" for i, v in enumerate(sets)}
    head = ["Engine", "HA provider", "Settings", "Test", "Runs"] + [m[1] for m in METRICS]
    lines = ["# Run comparison", "", "Median over repeats, (min-max) when repeated. Lost and consistency errors show the worst run.", "",
             "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    rows = []
    for (eng, ha, test, st), rs in sorted(groups.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][1], kv[0][3])):
        cells = [eng, ha, sid.get(st, "-"), test, str(len(rs))] + [fmt([r.get(k) for r in rs], sc, dc, ag) for k, _, sc, dc, ag in METRICS]
        lines.append("| " + " | ".join(cells) + " |")
        rows.append({"engine": eng, "ha_provider": ha, "settings": json.loads(st), "test": test, "runs": [r["run_id"] for r in rs],
                     **{col: cells[5 + i] for i, (_, col, *_rest) in enumerate(METRICS)}})
    if sets:
        lines += ["", "Settings:"] + [f"- {sid[v]}: `{v}`" for v in sets]
    lines += ["", "Runs: " + ", ".join(a.runs)]
    if missing:
        lines.append("Without results (failed or interrupted): " + ", ".join(missing))

    open(os.path.join(out, "compare.md"), "w").write("\n".join(lines) + "\n")
    glabel = lambda k: f"{k[0]} {k[1]}{' ' + sid[k[3]] if k[3] in sid else ''} {k[2]}"  # noqa: E731
    ordered = sorted(groups.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][1], kv[0][3]))
    timelines = [(f"{glabel(k)} - {r['run_id']}", os.path.join(RESULTS, r["run_id"], "timeline.svg"))
                 for k, rs in ordered for r in rs if os.path.exists(os.path.join(RESULTS, r["run_id"], "timeline.svg"))]
    html_report(os.path.join(out, "compare.html"), "Run comparison" + (f": {os.path.basename(out.rstrip('/'))}" if a.suite else ""),
                lines, [(glabel(k), rs) for k, rs in ordered], timelines)
    json.dump(rows, open(os.path.join(out, "compare.json"), "w"), indent=2)
    print("\n".join(lines))
    print(f"\n-> {os.path.relpath(os.path.join(out, 'compare.md'), ROOT)}, compare.html")


if __name__ == "__main__":
    main()
