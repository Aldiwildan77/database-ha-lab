#!/usr/bin/env python3
"""Writes <run>/timeline.svg: committed and failed transactions per second, p99 latency,
the fault window and the run's events on one time axis.  timeline.py <run_dir>"""

import json
import os
import sys
from html import escape

W, H, L, R, T, B = 1100, 360, 56, 56, 24, 64
OK, ERR, LAT, FAULT, GRID, INK = "#2b6cb0", "#c53030", "#805ad5", "#f6ad55", "#e2e8f0", "#2d3748"
SHOWN = ("fault_injected", "fault_active", "primary_unavailable", "manual_promotion_started", "new_primary_writable",
         "fault_restored", "all_nodes_healthy", "replication_converged")


def build(rows, events, chaos, t0):
    rows = [r for r in rows if r["start_ms"] / 1000 >= t0]
    if not rows:
        return None
    end = max(r["end_ms"] for r in rows) / 1000
    n = int(end - t0) + 1
    ok, err, lat = [0] * n, [0] * n, [[] for _ in range(n)]
    for r in rows:
        i = int(r["end_ms"] / 1000 - t0)
        if 0 <= i < n:
            (ok if r["ok"] else err)[i] += 1
            if r["ok"]:
                lat[i].append(r["txn_s"])
    p99 = [sorted(v)[int(len(v) * 0.99) - (1 if len(v) * 0.99 == int(len(v) * 0.99) else 0)] if v else None for v in lat]
    ymax = max(max(ok), max(err), 1)
    lmax = max([v for v in p99 if v] or [1])
    pw, ph = W - L - R, H - T - B
    x = lambda s: L + pw * s / max(n - 1, 1)  # noqa: E731
    y = lambda v: T + ph * (1 - v / ymax)  # noqa: E731
    yl = lambda v: T + ph * (1 - v / lmax)  # noqa: E731

    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="Helvetica,Arial,sans-serif" font-size="11">',
         f'<rect width="{W}" height="{H}" fill="#fff"/>']
    ev = {e["event"]: e["ts"] - t0 for e in events}
    f0 = ev.get("fault_active", ev.get("fault_injected"))
    if f0 is not None:
        f1 = ev.get("fault_restored", n)
        s.append(f'<rect x="{x(f0):.1f}" y="{T}" width="{max(x(f1) - x(f0), 1):.1f}" height="{ph}" fill="{FAULT}" opacity="0.22"/>')
    for k in range(5):
        gy = T + ph * k / 4
        s.append(f'<line x1="{L}" x2="{W - R}" y1="{gy:.1f}" y2="{gy:.1f}" stroke="{GRID}"/>')
        s.append(f'<text x="{L - 6}" y="{gy + 4:.1f}" text-anchor="end" fill="{INK}">{ymax * (1 - k / 4):.0f}</text>')
        s.append(f'<text x="{W - R + 6}" y="{gy + 4:.1f}" fill="{LAT}">{lmax * (1 - k / 4) * 1000:.0f}</text>')
    step = max(30, (n // 10 + 29) // 30 * 30)
    for t in range(0, n, step):
        s.append(f'<text x="{x(t):.1f}" y="{H - B + 16}" text-anchor="middle" fill="{INK}">{t // 60}:{t % 60:02d}</text>')
    for series, color in ((ok, OK), (err, ERR)):
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(series))
        s.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.4"/>')
    pts = " ".join(f"{x(i):.1f},{yl(v):.1f}" for i, v in enumerate(p99) if v is not None)
    s.append(f'<polyline points="{pts}" fill="none" stroke="{LAT}" stroke-width="1" stroke-dasharray="3 2"/>')
    marks = [(ev[k], k.replace("_", " ")) for k in SHOWN if k in ev] + [(c["t"], c["msg"]) for c in chaos]
    for j, (t, label) in enumerate(sorted(marks)):
        if 0 <= t <= n:
            s.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{T}" y2="{T + ph}" stroke="{INK}" stroke-width="0.6" stroke-dasharray="2 3"/>')
            s.append(f'<text x="{x(t) + 3:.1f}" y="{T + 12 + (j % 6) * 12}" fill="{INK}" font-size="9.5">{escape(label[:40])}</text>')
    legend = [(OK, "committed/s"), (ERR, "failed/s"), (LAT, "p99 latency ms (right axis)"), (FAULT, "fault window")]
    lx = L
    for color, text in legend:
        s.append(f'<rect x="{lx}" y="{H - 22}" width="12" height="8" fill="{color}"/>')
        s.append(f'<text x="{lx + 16}" y="{H - 14}" fill="{INK}">{text}</text>')
        lx += 30 + len(text) * 6
    s.append(f'<text x="{L}" y="{T - 8}" fill="{INK}" font-size="12" font-weight="bold">Timeline (mm:ss from load start)</text>')
    s.append("</svg>")
    return "\n".join(s)


def write(run_dir, rows=None):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "correlation"))
    import analyze  # noqa: E402
    meta = json.load(open(os.path.join(run_dir, "metadata.json")))
    load = lambda f: json.load(open(os.path.join(run_dir, f))) if os.path.exists(os.path.join(run_dir, f)) else []  # noqa: E731
    rows = rows if rows is not None else list(analyze.load_jtl(os.path.join(run_dir, "jmeter.jtl")))
    svg = build(rows, load("failover-events.json"), load("chaos-events.json"), meta["started_at_epoch"])
    if svg:
        open(os.path.join(run_dir, "timeline.svg"), "w").write(svg)
    return bool(svg)


if __name__ == "__main__":
    print(write(sys.argv[1]))
