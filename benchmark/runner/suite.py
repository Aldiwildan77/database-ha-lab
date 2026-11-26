#!/usr/bin/env python3
"""Runs a list of benchmarks from a suite file, each N times, against the topology deployed now,
then writes a comparison report. Suite file format: see benchmark/suites/example.yml.

  suite.py --suite benchmark/suites/example.yml [--repeat 3] [--reports] [--confirm yes]
"""

import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESULTS = os.path.join(ROOT, "benchmark", "results")


def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run_one(entry, confirm, log_path):
    cmd = [sys.executable, "-I", os.path.join(ROOT, "benchmark", "runner", "run.py"), "--scenario", entry["scenario"]]
    if entry.get("fault"):
        cmd += ["--fault", entry["fault"], "--confirm", confirm]
    for k, v in (entry.get("jmeter") or {}).items():
        cmd += ["-J", f"{k}={v}"]
    cmd += shlex.split(entry.get("args", ""))
    run_id = None
    with open(log_path, "a") as out:
        out.write(f"\n$ {shlex.join(cmd)}\n")
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            out.write(line)
            if not run_id and (m := re.search(r"run_id (run-\d{8}-\d+)", line)):
                run_id = m.group(1)
        rc = p.wait()
    return run_id, rc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suite", required=True)
    ap.add_argument("--repeat", type=int, help="overrides the suite file")
    ap.add_argument("--reports", action="store_true", help="also make screenshots + PDF per run (slow)")
    ap.add_argument("--confirm", default="")
    a = ap.parse_args()

    suite = yaml.safe_load(open(a.suite))
    entries, repeat = suite["runs"], a.repeat or int(suite.get("repeat", 1))
    if any(e.get("fault") for e in entries) and a.confirm != "yes":
        raise SystemExit("This suite injects faults. Re-run with CONFIRM=yes.")

    name = os.path.splitext(os.path.basename(a.suite))[0]
    suite_id = f"{name}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    sdir = os.path.join(RESULTS, "suites", suite_id)
    os.makedirs(sdir)
    state = {"suite": suite_id, "file": os.path.relpath(a.suite, ROOT), "repeat": repeat, "runs": []}
    save = lambda: json.dump(state, open(os.path.join(sdir, "suite.json"), "w"), indent=2)  # noqa: E731

    total, n = len(entries) * repeat, 0
    log(f"suite {suite_id}: {len(entries)} entries x {repeat} = {total} runs, log {os.path.relpath(sdir, ROOT)}/suite.log")
    for i in range(repeat):
        for entry in entries:
            n += 1
            label = entry.get("name") or "_".join(filter(None, [entry["scenario"], entry.get("fault")]))
            log(f"[{n}/{total}] {label} (repeat {i + 1})")
            run_id, rc = run_one(entry, a.confirm, os.path.join(sdir, "suite.log"))
            state["runs"].append({"label": label, "repeat": i + 1, "run_id": run_id, "rc": rc})
            save()
            log(f"  -> {run_id} rc={rc}")
            if run_id and a.reports:
                for t in ("screenshots", "pdf"):
                    subprocess.run(["make", t, f"RUN={run_id}"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    subprocess.run([sys.executable, "-I", os.path.join(ROOT, "benchmark", "report", "compare.py"), "--suite", sdir], cwd=ROOT)


if __name__ == "__main__":
    main()
