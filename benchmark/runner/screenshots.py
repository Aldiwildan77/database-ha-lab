#!/usr/bin/env python3
"""Captures every Grafana panel for one benchmark run's time window with headless Chrome.

Output: benchmark/results/<run>/screenshots/<dashboard>/<nn>-<panel>.png and index.md.
Needs Grafana anonymous viewer access (local compose, bound to 127.0.0.1)."""

import argparse
import concurrent.futures as cf
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHROMES = [os.environ.get("CHROME", ""), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
           "google-chrome", "chromium", "chromium-browser"]


def find_chrome():
    for c in CHROMES:
        if c and (os.path.exists(c) or shutil.which(c)):
            return c
    sys.exit("Chrome/Chromium not found; set CHROME=/path/to/chrome")


def panels(dash):
    out = []
    for p in dash.get("panels", []):
        out.append(p)
        out += p.get("panels", [])
    return [p for p in out if p.get("type") not in ("row", "text")]


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "panel"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="run id (default: latest)")
    ap.add_argument("--grafana", default=f"http://127.0.0.1:{os.environ.get('GRAFANA_PORT', '3000')}")
    ap.add_argument("--margin", type=float, default=60, help="seconds added before and after the run")
    ap.add_argument("--width", type=int, default=1200)
    ap.add_argument("--height", type=int, default=500)
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()

    results = os.path.join(ROOT, "benchmark", "results")
    run = a.run or (sorted(os.path.basename(d) for d in glob.glob(os.path.join(results, "run-*"))) or [None])[-1]
    if not run:
        sys.exit("no benchmark runs in benchmark/results")
    run_dir = os.path.join(results, run)
    meta = json.load(open(os.path.join(run_dir, "metadata.json")))
    start, end = meta.get("started_at_epoch"), meta.get("finished_at_epoch")
    if not start or not end:
        sys.exit(f"{run}: metadata.json has no start/finish time (run unfinished?)")
    t_from, t_to = int((start - a.margin) * 1000), int((end + a.margin) * 1000)

    try:
        urllib.request.urlopen(f"{a.grafana}/api/dashboards/uid/lab-ha-overview", timeout=5)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit("Grafana needs anonymous viewer access for screenshots: run make monitoring to apply it.")
        raise
    except OSError:
        sys.exit(f"Grafana not reachable at {a.grafana}: run make monitoring")

    variables = {"var-ha_provider": meta.get("ha_provider", ""), "var-scenario": meta.get("scenario", ""),
                 "var-environment": meta.get("environment", ""), "var-datacenter": "$__all", "var-node": "$__all"}
    chrome = find_chrome()
    out_root = os.path.join(run_dir, "screenshots")
    jobs = []
    for f in sorted(glob.glob(os.path.join(ROOT, "local", "grafana", "dashboards", "*.json"))):
        dash = json.load(open(f))
        name = os.path.splitext(os.path.basename(f))[0]
        os.makedirs(os.path.join(out_root, name), exist_ok=True)
        for i, p in enumerate(panels(dash), 1):
            q = {"orgId": 1, "panelId": p["id"], "from": t_from, "to": t_to, "theme": "light",
                 "width": a.width, "height": a.height, **variables}
            url = f"{a.grafana}/d-solo/{dash['uid']}/x?{urllib.parse.urlencode(q)}"
            png = os.path.join(out_root, name, f"{i:02d}-{slug(p.get('title', ''))}.png")
            jobs.append((dash.get("title", name), p.get("title", ""), url, png))

    def shoot(job):
        _, _, url, png = job
        r = subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                            f"--window-size={a.width},{a.height}", "--virtual-time-budget=15000",
                            f"--screenshot={png}", url], capture_output=True, timeout=90)
        return r.returncode == 0 and os.path.exists(png)

    print(f"{run}: {len(jobs)} panels, {meta.get('started_at')} .. {meta.get('finished_at')} (+/- {a.margin:.0f}s)")
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        ok = list(ex.map(shoot, jobs))

    lines = [f"# Screenshots: {run}", "",
             f"Scenario `{meta.get('scenario')}`, provider `{meta.get('ha_provider')}`, fault `{meta.get('fault')}`, "
             f"window {meta.get('started_at')} .. {meta.get('finished_at')} (+/- {a.margin:.0f} s).", ""]
    current = None
    for (dash_title, title, _, png), good in zip(jobs, ok):
        if dash_title != current:
            lines += ["", f"## {dash_title}", ""]
            current = dash_title
        rel = os.path.relpath(png, out_root)
        lines.append(f"### {title}\n\n![{title}]({rel})\n" if good else f"- {title}: capture failed")
    with open(os.path.join(out_root, "index.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"{sum(ok)}/{len(ok)} captured -> {os.path.relpath(out_root, ROOT)}/index.md")
    sys.exit(0 if all(ok) else 1)


if __name__ == "__main__":
    main()
