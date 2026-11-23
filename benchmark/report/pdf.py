#!/usr/bin/env python3
"""Builds benchmark/results/<run>/report.pdf: scenario, JMeter plan, report.md and every Grafana screenshot.

Run make screenshots first so the panels exist. Rendered with headless Chrome."""

import argparse
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "jmeter"))
from importlib import import_module  # noqa: E402

SCENARIOS = import_module("build-scenarios").SCENARIOS

PURPOSE = {
    "baseline": "Low, steady load to record the reference latency and throughput of the topology.",
    "read-heavy": "80 % reads: how the topology serves a read-dominated workload.",
    "write-heavy": "80 % writes: commit path, replication apply and lag under write pressure.",
    "mixed-workload": "Even read/write mix at 16 threads.",
    "spike-test": "Steady load plus a burst thread group: behaviour under a sudden load spike.",
    "soak-test": "One hour of mixed load: drift, leaks, lag growth over time.",
    "failover-test": "Write-heavy load while the primary is faulted: detection, promotion, RTO, RPO and rejoin.",
    "network-disruption": "Mixed load while netem delay/loss is applied to a node.",
}
DASH_ORDER = ["failover-recovery", "ha-overview", "replication-ha", "jmeter-benchmark", "latency-breakdown",
              "mysql-overview", "host-overview", "network"]
CHROMES = [os.environ.get("CHROME", ""), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
           "google-chrome", "chromium", "chromium-browser"]


def inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", s)


def md(text):
    """Small Markdown subset used by report.md: headings, tables, bullets, paragraphs."""
    out, rows, items = [], [], []

    def flush():
        if rows:
            head, body = rows[0], [r for r in rows[2:]]
            out.append("<table><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr>" +
                       "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body) + "</table>")
            rows.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for line in text.splitlines():
        if line.startswith("|"):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
            continue
        if line.startswith("- "):
            items.append(line[2:])
            continue
        flush()
        m = re.match(r"(#+) (.*)", line)
        if m:
            lvl = min(len(m.group(1)) + 1, 4)
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    flush()
    return "\n".join(out)


def kv_table(pairs):
    return "<table>" + "".join(f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
                               for k, v in pairs) + "</table>"


def scenario_html(meta, ctx):
    name = meta.get("scenario", "")
    ratio, threads, rampup, duration, extra = SCENARIOS.get(name, ("?", "?", "?", "?", []))
    props = meta.get("jmeter_properties", {})
    dur = float(props.get("duration", duration) or 0)
    warm = float(meta.get("warmup_s") or 0)
    fault = meta.get("fault") or "none"
    rows = [("Scenario", name), ("Purpose", PURPOSE.get(name, "")), ("HA provider", meta.get("ha_provider")),
            ("Database", f"{ctx.get('db_engine', '')} {ctx.get('db_version_config', '')}".strip()),
            ("Endpoint", f"{meta['endpoint'].get('type')} ({meta['endpoint'].get('target')}, mode {meta['endpoint'].get('mode')})"
             if isinstance(meta.get("endpoint"), dict) else meta.get("endpoint")),
            ("Client location", meta.get("client_location")), ("Initial primary", meta.get("initial_primary")),
            ("Final primary", meta.get("final_primary")), ("Warm-up excluded", f"{warm:.0f} s"),
            ("Fault", f"{fault} on {meta.get('fault_node')}" if fault != "none" else "none"),
            ("Git commit", meta.get("git_commit"))]
    steps = ["Load the dataset (prepare.jmx) unless --skip-prepare.",
             f"Start JMeter: {props.get('threads', threads)} threads, ramp-up {props.get('rampup', rampup)} s, "
             f"{dur:.0f} s total; the first {warm:.0f} s are excluded as warm-up."]
    if fault != "none":
        steps += [f"Inject {fault} on {meta.get('fault_node')} at about {max(warm + 10, dur / 3):.0f} s.",
                  "Detect primary loss; promote a replica (manual promotion unless the provider fails over itself).",
                  "Restore the fault after the fault duration; the old primary rejoins as a replica.",
                  "Wait for replication to converge, then verify the ledger on every node (RPO)."]
    else:
        steps.append("Verify the ledger on every node after the run.")
    return ("<h1>1. Scenario</h1>" + kv_table(rows) + "<h3>Procedure</h3><ol>" +
            "".join(f"<li>{html.escape(s)}</li>" for s in steps) + "</ol>")


def jmeter_html(meta):
    name = meta.get("scenario", "")
    ratio, threads, rampup, duration, extra = SCENARIOS.get(name, ("?", "?", "?", "?", []))
    props = meta.get("jmeter_properties", {})
    rpt = props.get("reads_per_txn", 5)
    groups = [("main", props.get("threads", threads), props.get("rampup", rampup), props.get("duration", duration))]
    groups += [(g, "see jmx", "", "") for g in extra]
    jmx = f"jmeter/scenarios/{name}.jmx"
    return (f"<h1>2. JMeter scenario</h1><p>Plan <code>{jmx}</code>, generated by <code>jmeter/build-scenarios.py</code>. "
            "Every sample is one database transaction run by <code>jmeter/scripts/transaction.groovy</code> "
            "through a topology-aware client that reconnects to the writable node.</p>"
            "<h3>Thread groups</h3><table><tr><th>Group</th><th>Threads</th><th>Ramp-up (s)</th><th>Duration (s)</th></tr>" +
            "".join(f"<tr><td>{g}</td><td>{t}</td><td>{r}</td><td>{d}</td></tr>" for g, t, r, d in groups) + "</table>"
            "<h3>Transaction mix</h3><table class='mix'><tr><th>Type</th><th>Share</th><th>Statements</th></tr>"
            f"<tr><td>read</td><td>{float(ratio) * 100:.0f} %</td><td>{rpt} &times; <code>SELECT id, balance, payload FROM accounts WHERE id = ?</code> (random id)</td></tr>"
            f"<tr><td>write</td><td>{(1 - float(ratio)) * 100:.0f} %</td><td><code>INSERT INTO ha_ledger (txn_id, run_id, thread, seq, client_epoch_ms, served_by, payload)</code>, "
            "<code>UPDATE accounts SET balance = balance + 1 WHERE id = ?</code>, <code>COMMIT</code></td></tr></table>"
            "<p>Each acknowledged write leaves a ledger row; after the run the ledger on every node is compared with the "
            "client's list of acknowledged commits to measure lost, phantom and duplicate writes.</p>"
            "<h3>JMeter properties used</h3>" + kv_table(sorted(props.items())))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="run id (default: latest)")
    a = ap.parse_args()
    results = os.path.join(ROOT, "benchmark", "results")
    run = a.run or (sorted(os.path.basename(d) for d in glob.glob(os.path.join(results, "run-*"))) or [None])[-1]
    if not run:
        sys.exit("no benchmark runs in benchmark/results")
    rd = os.path.join(results, run)
    meta = json.load(open(os.path.join(rd, "metadata.json")))
    ctx = json.load(open(os.path.join(rd, "context.json"))) if os.path.exists(os.path.join(rd, "context.json")) else {}
    report = open(os.path.join(rd, "report.md")).read() if os.path.exists(os.path.join(rd, "report.md")) else ""
    shots = os.path.join(rd, "screenshots")
    if not glob.glob(os.path.join(shots, "*", "*.png")):
        print(f"{run}: no screenshots yet (run make screenshots); the PDF will have none")

    sections = [scenario_html(meta, ctx), jmeter_html(meta),
                "<h1>3. Results</h1>" + md(re.sub(r"\A# .*\n", "", report))]
    titles, panel = {}, {}
    idx = os.path.join(shots, "index.md")
    if os.path.exists(idx):
        cur = None
        for line in open(idx):
            if line.startswith("## "):
                cur = line[3:].strip()
            m = re.match(r"!\[(.*)\]\((([^/]+)/[^)]+)\)", line)
            if m and cur:
                titles.setdefault(m.group(3), cur)
                panel[m.group(2)] = m.group(1)
    dirs = sorted((d for d in os.listdir(shots) if os.path.isdir(os.path.join(shots, d))),
                  key=lambda d: DASH_ORDER.index(d) if d in DASH_ORDER else 99) if os.path.isdir(shots) else []
    n = 0
    figs = ["<h1>4. Grafana panels</h1><p>Each panel covers the run window &plusmn; 60 s. "
            "Panels for other HA providers (semi-sync, group replication, Galera) show no data.</p>"]
    for d in dirs:
        figs.append(f"<h2 class='pb'>4.{dirs.index(d) + 1} {html.escape(titles.get(d, d))}</h2>")
        for png in sorted(glob.glob(os.path.join(shots, d, "*.png"))):
            n += 1
            cap = panel.get(os.path.relpath(png, shots)) or os.path.basename(png)[:-4]
            figs.append(f"<figure><img src='file://{png}'><figcaption>Figure {n}. {html.escape(cap)}</figcaption></figure>")
    sections.append("\n".join(figs))

    title = f"{meta.get('scenario')} · {meta.get('ha_provider')} · {run}"
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title><style>
@page {{ size: A4; margin: 16mm 14mm; }}
body {{ font: 10pt/1.45 -apple-system, "Helvetica Neue", Arial, sans-serif; color: #1a1a1a; }}
.cover {{ height: 250mm; display: flex; flex-direction: column; justify-content: center; }}
.cover h1 {{ font-size: 26pt; margin: 0 0 8pt; border: 0; }}
.cover p {{ font-size: 12pt; color: #555; margin: 2pt 0; }}
h1 {{ font-size: 17pt; border-bottom: 2px solid #1a1a1a; padding-bottom: 3pt; margin-top: 0; }}
h2 {{ font-size: 13.5pt; margin: 14pt 0 6pt; }} h3, h4 {{ font-size: 11pt; margin: 12pt 0 4pt; }}
.pb {{ break-before: page; }}
table {{ border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 8.5pt; }}
th, td {{ border: 1px solid #ccc; padding: 3pt 5pt; text-align: left; vertical-align: top; word-break: break-word; }}
th {{ background: #f2f2f2; }}
table.mix td:nth-child(-n+2), table.mix th:nth-child(-n+2) {{ white-space: nowrap; width: 1%; }}
code {{ font: 8.5pt Menlo, monospace; background: #f4f4f4; padding: 0 2pt; }}
figure {{ margin: 0 0 10pt; break-inside: avoid; }} img {{ width: 100%; border: 1px solid #ddd; }}
figcaption {{ font-size: 8.5pt; color: #555; margin-top: 2pt; }}
</style></head><body>
<div class="cover"><h1>HA benchmark report</h1>
<p><b>{html.escape(str(meta.get('scenario')))}</b> on <b>{html.escape(str(meta.get('ha_provider')))}</b></p>
<p>Run {run} · {html.escape(str(meta.get('started_at')))} to {html.escape(str(meta.get('finished_at')))}</p>
<p>Environment {html.escape(str(meta.get('environment')))} · {n} Grafana panels</p></div>
<div class="pb"></div>{''.join(f'<section class="pb">{s}</section>' if i else s for i, s in enumerate(sections))}
</body></html>"""
    src = os.path.join(rd, "report.html")
    with open(src, "w") as fh:
        fh.write(doc)
    chrome = next((c for c in CHROMES if c and (os.path.exists(c) or shutil.which(c))), None)
    if not chrome:
        sys.exit("Chrome/Chromium not found; set CHROME=/path/to/chrome")
    pdf = os.path.join(rd, "report.pdf")
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--allow-file-access-from-files",
                    f"--print-to-pdf={pdf}", f"file://{src}"], capture_output=True, timeout=300, check=True)
    print(os.path.relpath(pdf, ROOT))


if __name__ == "__main__":
    main()
