#!/usr/bin/env python3
"""Builds reports/<db>/<ha_provider>/<scenario>/<db>-<ver>_<ha>_<scenario>[_<fault>]_<date>-<seq>.pdf for one run:
summary KPIs, scenario, JMeter plan, report.md and every Grafana screenshot. IP addresses are replaced with node
names unless report.redact_addresses is false. Run make screenshots first. Rendered with headless Chrome."""

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


def report_config():
    try:
        import yaml
        cfg = yaml.safe_load(open(os.path.join(ROOT, "config", "benchmark.yml"))) or {}
    except (ImportError, OSError):
        cfg = {}
    return {"author": "", "organization": "", "location": "", "redact_addresses": True, **(cfg.get("report") or {})}


def kpis(res, meta):
    def card(label, value, tone, note=""):
        return f"<div class='kpi {tone}'><div class='v'>{value}</div><div class='l'>{label}</div><div class='n'>{note}</div></div>"

    def tone(v, good, warn, lower=True):
        if v is None:
            return "neutral"
        return ("good" if v <= good else "warn" if v <= warn else "bad") if lower else \
               ("good" if v >= good else "warn" if v >= warn else "bad")

    s, out = res.get("summary", {}), []
    lost, rto = res.get("lost_acknowledged_writes"), res.get("rto_s")
    if meta.get("fault") and meta.get("fault") != "none":
        out.append(card("RPO: acknowledged writes lost", "-" if lost is None else lost, "good" if lost == 0 else "bad"))
        out.append(card("RTO: fault to first write", "-" if rto is None else f"{rto:.1f} s", tone(rto, 10, 30)))
        out.append(card("Failure detection", f"{res['detection_s']:.1f} s" if res.get("detection_s") else "-",
                        tone(res.get("detection_s"), 5, 15)))
    ce = res.get("consistency_errors")
    out.append(card("Consistency errors", "-" if ce is None else ce, "good" if ce == 0 else "bad"))
    wa = s.get("write_availability")
    out.append(card("Write availability", f"{wa * 100:.2f} %" if wa is not None else "-", tone(wa, 0.999, 0.95, False)))
    er = res.get("error_rate")
    out.append(card("Error rate", f"{er * 100:.2f} %" if er is not None else "-", tone(er, 0.001, 0.05)))
    out.append(card("TPS committed", f"{res['tps']:.1f}" if res.get("tps") else "-", "neutral",
                    f"writes {s.get('write_tps', 0):.1f} / reads {s.get('read_tps', 0):.1f}"))
    p50, p99 = res.get("latency_p50_s"), res.get("latency_p99_s")
    out.append(card("Latency p50 / p99", f"{p50 * 1000:.0f} / {p99 * 1000:.0f} ms" if p50 and p99 else "-", "neutral",
                    "client, incl. network"))
    return "<div class='kpis'>" + "".join(out) + "</div>"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="run id (default: latest)")
    a = ap.parse_args()
    results = os.path.join(ROOT, "benchmark", "results")
    run = a.run or (sorted(os.path.basename(d) for d in glob.glob(os.path.join(results, "run-*"))) or [None])[-1]
    if not run:
        sys.exit("no benchmark runs in benchmark/results")
    rd = os.path.join(results, run)

    def load(name, default):
        p = os.path.join(rd, name)
        return (json.load(open(p)) if name.endswith(".json") else open(p).read()) if os.path.exists(p) else default

    meta, ctx, res, report = load("metadata.json", None), load("context.json", {}), load("results.json", {}), load("report.md", "")
    if meta is None:
        sys.exit(f"{run}: no metadata.json")
    rc = report_config()
    shots = os.path.join(rd, "screenshots")
    if not glob.glob(os.path.join(shots, "*", "*.png")):
        print(f"{run}: no screenshots yet (run make screenshots); the PDF will have none")

    db = ctx.get("db_engine") or meta.get("jmeter_properties", {}).get("db_engine", "db")
    dbv = str(ctx.get("db_version_config") or "")
    ha, scen = meta.get("ha_provider", "ha"), meta.get("scenario", "scenario")
    fault = meta.get("fault") if meta.get("fault") not in (None, "none") else ""
    day = run.split("-")[1] if run.count("-") >= 2 else ""
    seq = run.split("-")[-1]
    stem = "_".join(x for x in [f"{db}-{dbv}" if dbv else db, ha, scen, fault, f"{day}-{seq}"] if x)
    out_dir = os.path.join(ROOT, "reports", db, ha, scen)
    os.makedirs(out_dir, exist_ok=True)

    sections = ["<h1>1. Summary</h1>" + kpis(res, meta) + scenario_html(meta, ctx).replace("<h1>1. Scenario</h1>", "<h2>Scenario</h2>"),
                jmeter_html(meta),
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
    figs = ["<h1>4. Grafana panels</h1><p>Each panel covers the run window &plusmn; 60 s. Stat panels show the value "
            "at the end of the window; read the sparkline behind it. Panels for other HA providers show no data.</p>"]
    for i, d in enumerate(dirs, 1):
        figs.append(f"<h2 class='pb dash'>4.{i} {html.escape(titles.get(d, d))}</h2>")
        for png in sorted(glob.glob(os.path.join(shots, d, "*.png"))):
            n += 1
            cap = panel.get(os.path.relpath(png, shots)) or os.path.basename(png)[:-4]
            figs.append(f"<figure><img src='file://{png}'><figcaption><b>Figure {n}.</b> {html.escape(cap)}</figcaption></figure>")
    sections.append("\n".join(figs))

    e = lambda v: html.escape(str(v or ""))  # noqa: E731
    credit = "".join(f"<div>{e(rc[k])}</div>" for k in ("organization", "location") if rc.get(k))
    credit = f"<div class='credit'><div class='who'>{e(rc['author'])}</div>{credit}</div>" if rc.get("author") else ""
    title = f"{ {'mysql': 'MySQL', 'mariadb': 'MariaDB', 'postgresql': 'PostgreSQL'}.get(db, db)} {dbv}"
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>{e(stem)}</title><style>
@page {{ size: A4; margin: 16mm 14mm 18mm; }}
:root {{ --ink: #1b2430; --muted: #5b6573; --accent: #1f5fae; --accent-soft: #e8f0fb; --rule: #d5dbe3;
        --good: #1e8e3e; --good-soft: #e6f4ea; --warn: #b26a00; --warn-soft: #fff4e0; --bad: #c5221f; --bad-soft: #fce8e6; }}
body {{ font: 10pt/1.45 -apple-system, "Helvetica Neue", Arial, sans-serif; color: var(--ink); }}
.cover {{ height: 255mm; display: flex; flex-direction: column; justify-content: space-between; }}
.band {{ background: var(--accent); color: #fff; padding: 22mm 14mm 14mm; margin: -16mm -14mm 0; width: calc(100% + 28mm); box-sizing: border-box; }}
.band .sub {{ font-size: 17pt; font-weight: 600; opacity: 1; margin-bottom: 8pt; }}
.band .k {{ text-transform: uppercase; letter-spacing: .14em; font-size: 9pt; opacity: .85; }}
.band h1 {{ font-size: 28pt; margin: 6pt 0 4pt; border: 0; color: #fff; padding: 0; }}
.band p {{ margin: 2pt 0; font-size: 12pt; opacity: .95; }}
.meta {{ margin-top: 14mm; }} .meta td, .meta th {{ border: 0; border-bottom: 1px solid var(--rule); font-size: 10pt; padding: 5pt 4pt; }}
.meta th {{ background: none; color: var(--muted); width: 32%; font-weight: 500; }}
.credit {{ border-left: 4px solid var(--accent); padding: 4pt 0 4pt 10pt; color: var(--muted); font-size: 11pt; }}
.credit .who {{ color: var(--ink); font-weight: 700; font-size: 13pt; }}
h1 {{ font-size: 17pt; color: var(--accent); border-bottom: 2px solid var(--accent); padding-bottom: 3pt; margin-top: 0; }}
h2 {{ font-size: 13.5pt; margin: 14pt 0 6pt; color: var(--ink); }} h3, h4 {{ font-size: 11pt; margin: 12pt 0 4pt; color: var(--accent); }}
h2.dash {{ background: var(--accent-soft); border-left: 4px solid var(--accent); padding: 4pt 8pt; margin-top: 0; }}
.pb {{ break-before: page; }}
table {{ border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 8.5pt; }}
th, td {{ border: 1px solid var(--rule); padding: 3pt 5pt; text-align: left; vertical-align: top; word-break: break-word; }}
th {{ background: var(--accent-soft); color: var(--ink); }}
tr:nth-child(even) td {{ background: #f8fafc; }}
table.mix td:nth-child(-n+2), table.mix th:nth-child(-n+2) {{ white-space: nowrap; width: 1%; }}
code {{ font: 8.5pt Menlo, monospace; background: #eef1f5; padding: 0 2pt; border-radius: 2pt; }}
.kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 6pt; margin: 4pt 0 12pt; }}
.kpi {{ border-radius: 5pt; padding: 7pt 8pt; border-top: 4px solid var(--muted); background: #f4f6f9; break-inside: avoid; }}
.kpi .v {{ font-size: 15pt; font-weight: 700; }} .kpi .l {{ font-size: 8pt; color: var(--muted); }} .kpi .n {{ font-size: 7.5pt; color: var(--muted); }}
.kpi.good {{ background: var(--good-soft); border-color: var(--good); }} .kpi.good .v {{ color: var(--good); }}
.kpi.warn {{ background: var(--warn-soft); border-color: var(--warn); }} .kpi.warn .v {{ color: var(--warn); }}
.kpi.bad {{ background: var(--bad-soft); border-color: var(--bad); }} .kpi.bad .v {{ color: var(--bad); }}
.kpi.neutral {{ border-color: var(--accent); }}
figure {{ margin: 0 0 10pt; break-inside: avoid; }} img {{ width: 100%; border: 1px solid var(--rule); border-radius: 3pt; }}
figcaption {{ font-size: 8.5pt; color: var(--muted); margin-top: 2pt; }}
</style></head><body>
<div class="cover"><div><div class="band"><div class="k">Database HA Lab · benchmark report</div><h1>{e(title)}</h1><p class="sub">{e(ha)} · {e(scen)}</p>
<p>Fault: {e(fault or 'none')}{' on ' + e(meta.get('fault_node')) if fault else ''}</p><p>{e(run)}</p></div>
<table class="meta">
<tr><th>Database</th><td>{e(db)} {e(dbv)}</td></tr><tr><th>HA provider</th><td>{e(ha)}</td></tr>
<tr><th>Scenario</th><td>{e(scen)}</td></tr><tr><th>Window (UTC)</th><td>{e(meta.get('started_at'))} to {e(meta.get('finished_at'))}</td></tr>
<tr><th>Environment</th><td>{e(meta.get('environment'))}, client {e(meta.get('client_location'))}</td></tr>
<tr><th>Primary</th><td>{e(meta.get('initial_primary'))} &rarr; {e(meta.get('final_primary'))}</td></tr>
<tr><th>Grafana panels</th><td>{n}</td></tr><tr><th>Git commit</th><td>{e(meta.get('git_commit'))}</td></tr></table></div>
{credit}</div>
{''.join(f'<section class="pb">{s}</section>' for s in sections)}
</body></html>"""
    if rc.get("redact_addresses", True):
        names = {**{v: k for k, v in (ctx.get("addresses") or {}).items()}}
        for node, nets in (ctx.get("networks") or {}).items() if isinstance(ctx.get("networks"), dict) else []:
            for ip in (nets.values() if isinstance(nets, dict) else []):
                if isinstance(ip, str):
                    names.setdefault(ip, node)
        for ip, node in sorted(names.items(), key=lambda x: -len(x[0])):
            doc = doc.replace(ip, node)
        doc = re.sub(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", "x.x.x.x", doc)
    src = os.path.join(rd, "report.html")
    with open(src, "w") as fh:
        fh.write(doc)
    chrome = next((c for c in CHROMES if c and (os.path.exists(c) or shutil.which(c))), None)
    if not chrome:
        sys.exit("Chrome/Chromium not found; set CHROME=/path/to/chrome")
    pdf = os.path.join(out_dir, stem + ".pdf")
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--allow-file-access-from-files",
                    f"--print-to-pdf={pdf}", f"file://{src}"], capture_output=True, timeout=300, check=True)
    print(os.path.relpath(pdf, ROOT))


if __name__ == "__main__":
    main()
