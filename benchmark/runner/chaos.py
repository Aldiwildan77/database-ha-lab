#!/usr/bin/env python3
"""Runs one benchmark and applies a fault plan during it.

  chaos.py --scenario spike-test --plan "180:kill-db 360:kill-db 540:freeze/60@lab-ivan" --confirm yes [-J duration=600]

Plan item: <seconds after load start>:<fault>[/<duration s>][@<node>]. The node defaults to the current primary.
stop-db / kill-db: wait --promote-after, promote another node, rebuild the old one as a replica.
Other faults: restored after their duration (default 60 s); a frozen or cut-off primary is not promoted.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run as lab  # noqa: E402

CRASH = ("stop-db", "kill-db")


def parse_plan(text):
    items = []
    for tok in text.split():
        m = re.fullmatch(r"(\d+):([a-z-]+)(?:/(\d+))?(?:@([\w.-]+))?", tok)
        if not m or m.group(2) not in lab.FAULTS or m.group(2) == "none":
            raise SystemExit(f"bad plan item '{tok}': <s>:<fault>[/<duration>][@<node>], faults: {lab.FAULTS[1:]}")
        items.append({"at": int(m.group(1)), "fault": m.group(2), "duration": int(m.group(3) or 60), "node": m.group(4)})
    return sorted(items, key=lambda i: i["at"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("-J", "--prop", action="append", default=[])
    ap.add_argument("--promote-after", type=float, default=10)
    ap.add_argument("--confirm", default="")
    ap.add_argument("--netem")
    a = ap.parse_args()
    plan = parse_plan(a.plan)
    if a.confirm != "yes":
        raise SystemExit("A chaos plan disrupts the cluster. Re-run with CONFIRM=yes.")

    cmd = [sys.executable, "-I", os.path.join(lab.ROOT, "benchmark", "runner", "run.py"), "--scenario", a.scenario]
    for p in a.prop:
        cmd += ["-J", p]
    bench = subprocess.Popen(cmd, cwd=lab.ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    run_id = None
    for line in bench.stdout:
        print(line, end="", flush=True)
        if m := re.search(r"run_id (run-\d{8}-\d+)", line):
            run_id = m.group(1)
            break
    if not run_id:
        raise SystemExit("benchmark did not start")
    threading.Thread(target=lambda: [print(ln, end="", flush=True) for ln in bench.stdout], daemon=True).start()
    run_dir = os.path.join(lab.ROOT, "benchmark", "results", run_id)
    lab.ANSIBLE_LOG_DIR = run_dir

    while not os.path.exists(os.path.join(run_dir, "jmeter.out")):
        if bench.poll() is not None:
            raise SystemExit("benchmark ended before load started")
        time.sleep(0.5)
    t0, events = time.time(), []
    ctx = json.load(open(os.path.join(run_dir, "context.json")))

    def note(msg):
        events.append({"at": dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S"), "t": time.time() - t0, "msg": msg})
        json.dump(events, open(os.path.join(run_dir, "chaos-events.json"), "w"), indent=2)
        lab.log(f"chaos: {msg}")

    def primary():
        for n, addr in ctx["addresses"].items():
            m = lab.scrape(addr, ctx["collector_port"])
            if m and m.get("ha_lab_writable") == 1:
                return n
        return None

    for it in plan:
        while time.time() - t0 < it["at"]:
            if bench.poll() is not None:
                break
            time.sleep(0.5)
        if bench.poll() is not None:
            note("benchmark ended, rest of the plan skipped")
            break
        node = it["node"] or primary()
        if not node:
            note(f"{it['fault']} skipped: no writable primary")
            continue
        base = {"node": node, "fault": it["fault"], "confirm": "yes", "max_seconds": it["duration"] + 300,
                **({"netem": a.netem} if a.netem else {})}
        note(f"{it['fault']} on {node}")
        lab.ansible("fault.yml", {**base, "action": "inject"}, check=False)
        if it["fault"] in CRASH:
            time.sleep(a.promote_after)
            p = lab.ansible("failover.yml", {"failover_mode": "failover"}, check=False)
            note(f"promoted {primary()}" if p.returncode == 0 else f"promotion failed rc={p.returncode}")
            p = lab.ansible("recover.yml", {"node": node, "rebuild": True}, check=False)
            note(f"{node} rebuilt as replica" if p.returncode == 0 else f"rebuild of {node} failed rc={p.returncode}")
        else:
            time.sleep(it["duration"])
            lab.ansible("fault.yml", {**base, "action": "restore"}, check=False)
            note(f"{it['fault']} on {node} restored")

    rc = bench.wait()
    note(f"benchmark finished rc={rc}")
    import report  # noqa: E402
    report.build(run_dir, os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090"))
    lab.log(f"report: benchmark/results/{run_id}/report.md")


if __name__ == "__main__":
    main()
