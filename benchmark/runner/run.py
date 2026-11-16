#!/usr/bin/env python3
"""Benchmark runner: one run = one run_id directory under benchmark/results/.

  run.py --scenario mixed-workload
  run.py --scenario failover-test --fault stop-db --confirm yes
  run.py --scenario network-disruption --fault netem --netem "delay 150ms 20ms loss 2%" --confirm yes

Produces metadata.json, context.json, network.json, jmeter.jtl, failover-events.json, ledger/,
consistency.json, prometheus-summary.json, results.json, report.md.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "metrics", "collectors"))
sys.path.insert(0, os.path.join(ROOT, "benchmark", "report"))
import netprobe  # noqa: E402

SCENARIOS = ["baseline", "read-heavy", "write-heavy", "mixed-workload", "spike-test", "soak-test",
             "failover-test", "network-disruption"]
FAULT_ALIASES = {"stop-mysql": "stop-db", "kill-mysql": "kill-db"}   # old names still accepted
FAULTS = ["none", "stop-db", "kill-db", "isolate", "netem", *FAULT_ALIASES]
SCENARIO_FAULT = {"failover-test": "stop-db", "network-disruption": "netem"}
SERVER_FAULTS = ("stop-db", "kill-db", "isolate")


def now():
    return time.time()


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="milliseconds")


def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def ansible(playbook, extra: dict, check=True):
    cmd = ["ansible-playbook", os.path.join(ROOT, "ansible", "playbooks", playbook)]
    cmd += os.environ.get("VAULT_ARGS", "").split()
    if extra:
        cmd += ["-e", json.dumps(extra)]
    env = {**os.environ, "ANSIBLE_CONFIG": os.path.join(ROOT, "ansible.cfg")}
    log("ansible-playbook " + playbook + " " + " ".join(f"{k}={v}" for k, v in extra.items() if k != "confirm"))
    p = subprocess.run(cmd, env=env, cwd=ROOT, capture_output=True, text=True)
    if check and p.returncode != 0:
        sys.stderr.write(p.stdout[-4000:] + p.stderr[-2000:])
        raise SystemExit(f"{playbook} failed (rc={p.returncode})")
    return p


def new_run_id(results):
    day = dt.datetime.now().strftime("%Y%m%d")
    n = 1
    if os.path.isdir(results):
        nums = [int(m.group(1)) for d in os.listdir(results) if (m := re.match(rf"run-{day}-(\d+)$", d))]
        n = max(nums, default=0) + 1
    return f"run-{day}-{n:03d}"


class Events:
    def __init__(self, path):
        self.path, self.items, self.lock = path, [], threading.Lock()

    def add(self, event, ts=None, **kw):
        ts = ts or now()
        with self.lock:
            if any(e["event"] == event for e in self.items) and not kw.pop("repeat", False):
                return
            self.items.append({"event": event, "ts": ts, "at": iso(ts), **kw})
            self.items.sort(key=lambda e: e["ts"])
            json.dump(self.items, open(self.path, "w"), indent=2)
        log(f"event {event} {kw or ''}")

    def has(self, event):
        return any(e["event"] == event for e in self.items)


def scrape(addr, port):
    host = f"[{addr}]" if ":" in addr else addr
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/metrics", timeout=1.5) as r:
            text = r.read().decode()
    except Exception:
        return None
    out = {}
    for line in text.splitlines():
        if line.startswith("ha_lab_") and "{" not in line:
            k, _, v = line.partition(" ")
            out[k] = float(v)
    return out


class Poller(threading.Thread):
    """Polls every node's ha-lab collector to time detection, promotion, convergence and recovery."""

    def __init__(self, ctx, events: Events, interval=0.5):
        super().__init__(daemon=True)
        self.ctx, self.events, self.interval = ctx, events, interval
        self.stop_flag = threading.Event()
        self.fault_node = None
        self.state = {}

    def healthy(self, m):
        if not m or m.get("ha_lab_db_up") != 1:
            return False
        return (m.get("ha_lab_writable") == 1 or
                (m.get("ha_lab_replica_io_running") == 1 and m.get("ha_lab_replica_sql_running") == 1) or
                m.get("ha_lab_group_member_online") == 1 or m.get("ha_lab_wsrep_ready") == 1)

    def run(self):
        port = self.ctx["collector_port"]
        while not self.stop_flag.is_set():
            t = now()
            snap = {n: scrape(a, port) for n, a in self.ctx["addresses"].items()}
            self.state = snap
            fn = self.fault_node
            if fn and self.events.has("fault_injected"):
                m = snap.get(fn)
                if m is None or m.get("ha_lab_writable") != 1:
                    self.events.add("primary_unavailable", t, node=fn,
                                    reason="collector unreachable" if m is None else "not writable")
                others = [n for n, mm in snap.items() if n != fn and mm and mm.get("ha_lab_writable") == 1]
                if others:
                    self.events.add("new_primary_writable", t, node=others[0], all_writable=others)
                if self.events.has("fault_restored") or self.events.has("new_primary_writable"):
                    up = [mm for mm in snap.values() if mm and mm.get("ha_lab_db_up") == 1]
                    counts = {mm.get("ha_lab_replication_position") for mm in up}
                    if len(up) >= 2 and len(counts) == 1:
                        self.events.add("replication_converged", t, nodes=len(up))
                if self.events.has("fault_restored") and all(self.healthy(mm) for mm in snap.values()):
                    self.events.add("all_nodes_healthy", t)
            self.stop_flag.wait(max(0, self.interval - (now() - t)))

    def writable(self):
        return [n for n, m in self.state.items() if m and m.get("ha_lab_writable") == 1]

    def converged(self):
        up = [m for m in self.state.values() if m and m.get("ha_lab_db_up") == 1]
        return len({m.get("ha_lab_replication_position") for m in up}) <= 1


def client_network(ctx, samples=10):
    out = []
    ntype = {n["name"]: n.get("network_type", "tailscale") for n in ctx["nodes"]}
    for name, addr in ctx["addresses"].items():
        rtts, sent, ok = netprobe.ping(addr, count=samples)
        tcp = [d for d in (netprobe.tcp_connect(addr, ctx.get("db_port", ctx["mysql_port"])) for _ in range(5)) if d is not None]
        direct, ts_rtt, path = netprobe.tailscale_ping(addr) if ntype.get(name) == "tailscale" else (None, None, "")
        srt = sorted(rtts)
        out.append({"node": name, "address": addr, "rtt_avg_s": sum(rtts) / len(rtts) if rtts else None,
                    "rtt_p50_s": srt[len(srt) // 2] if srt else None, "rtt_min_s": srt[0] if srt else None,
                    "rtt_max_s": srt[-1] if srt else None, "loss_ratio": (sent - ok) / sent if sent else None,
                    "tcp_connect_avg_s": sum(tcp) / len(tcp) if tcp else None,
                    "network_type": ntype.get(name), "tailscale_direct": direct, "tailscale_rtt_s": ts_rtt,
                    "path": f"tailscale {path}" if path else ntype.get(name)})
    return out


def grafana_annotation(run_id, scenario, start_ms, end_ms=None, ann_id=None):
    env = os.path.join(ROOT, "local", "compose", ".env")
    if not os.path.exists(env):
        return None
    pw = next((l.split("=", 1)[1].strip() for l in open(env) if l.startswith("GRAFANA_ADMIN_PASSWORD=")), "")
    auth = base64.b64encode(f"admin:{pw}".encode()).decode()
    body = {"time": start_ms, "tags": ["benchmark", scenario, run_id], "text": f"{run_id} ({scenario})"}
    if end_ms:
        body["timeEnd"] = end_ms
    req = urllib.request.Request(
        f"http://127.0.0.1:{os.environ.get('GRAFANA_PORT', '3000')}/api/annotations" + (f"/{ann_id}" if ann_id else ""),
        data=json.dumps(body).encode(), method="PUT" if ann_id else "POST",
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.load(r).get("id", ann_id)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", required=True, choices=SCENARIOS)
    ap.add_argument("--endpoint", help="ha | primary | <node name>; default from config/benchmark.yml")
    ap.add_argument("-J", "--prop", action="append", default=[], help="JMeter property override k=v")
    ap.add_argument("--fault", choices=FAULTS, help="default: stop-db for failover-test, netem for network-disruption (stop-mysql/kill-mysql are aliases)")
    ap.add_argument("--fault-node", help="default: the current primary")
    ap.add_argument("--fault-at", type=float, help="seconds after load start (default: 1/3 of duration)")
    ap.add_argument("--fault-duration", type=float, default=60, help="seconds until the fault is restored")
    ap.add_argument("--netem", default="delay 100ms 20ms loss 1%")
    ap.add_argument("--promote-after", type=float, default=None,
                    help="async/semi-sync: seconds after detection to run manual promotion (default from config, 10)")
    ap.add_argument("--no-promote", action="store_true", help="async/semi-sync: never promote; measure the outage")
    ap.add_argument("--confirm", default="", help="'yes' is required for faults")
    ap.add_argument("--warmup", type=float, default=None)
    ap.add_argument("--skip-prepare", action="store_true")
    ap.add_argument("--skip-network", action="store_true")
    ap.add_argument("--iperf", action="store_true")
    ap.add_argument("--prometheus", default=os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090"))
    a = ap.parse_args()

    fault = a.fault or SCENARIO_FAULT.get(a.scenario, "none")
    fault = FAULT_ALIASES.get(fault, fault)
    if fault != "none" and a.confirm != "yes":
        raise SystemExit(f"Fault '{fault}' disrupts the cluster. Re-run with --confirm yes (make ... CONFIRM=yes).")

    jmeter = os.path.join(ROOT, "local", "tools", "jmeter", "bin", "jmeter")
    if not os.path.exists(jmeter):
        raise SystemExit("JMeter missing: run make jmeter-install")

    results = os.path.join(ROOT, "benchmark", "results")
    run_id = new_run_id(results)
    run_dir = os.path.join(results, run_id)
    os.makedirs(os.path.join(run_dir, "ledger"))
    log(f"run_id {run_id} -> {os.path.relpath(run_dir, ROOT)}")

    ansible("benchmark-context.yml", {"out": run_dir, **({"endpoint_override": a.endpoint} if a.endpoint else {})})
    ctx = json.load(open(os.path.join(run_dir, "context.json")))
    bench = ctx.get("benchmark_config") or {}
    load_cfg = {**(bench.get("load") or {}), **((bench.get("scenarios") or {}).get(a.scenario) or {})}
    dataset = bench.get("dataset") or {}

    props = {
        "run_id": run_id, "endpoints": ",".join(ctx["endpoint"]["endpoints"]), "endpoint_mode": ctx["endpoint"]["mode"],
        "database": ctx["database"], "db_engine": ctx.get("db_engine", "mysql"), "lab_script_dir": os.path.join(ROOT, "jmeter", "scripts"),
        "ssl_mode": ctx.get("ssl_mode", "PREFERRED"),
        "rows": dataset.get("rows", 100000), "seed": dataset.get("seed", 42),
    }
    keymap = {"threads": "threads", "ramp_up_seconds": "rampup", "duration_seconds": "duration", "read_ratio": "read_ratio",
              "reads_per_txn": "reads_per_txn", "think_ms": "think_ms", "spike_threads": "spike_threads",
              "spike_at_seconds": "spike_at", "spike_duration_seconds": "spike_duration",
              "socket_timeout_ms": "socket_timeout_ms", "connect_timeout_ms": "connect_timeout_ms",
              "reconnect_backoff_ms": "reconnect_backoff_ms"}
    for k, v in load_cfg.items():
        if k in keymap:
            props[keymap[k]] = v
    for kv in a.prop:
        k, _, v = kv.partition("=")
        props[k] = v
    duration = float(props.get("duration", 300))
    warmup = a.warmup if a.warmup is not None else float(props.get("rampup", 0))

    secret = os.path.join(run_dir, "secret.properties")
    jcommon = [jmeter, "-n", "-q", os.path.join(ROOT, "jmeter", "lab.properties"), "-q", secret]
    jcommon += [f"-J{k}={v}" for k, v in props.items()]

    network = {"measured_at": iso(now()), "client_location": ctx["client_location"]}
    if not a.skip_network:
        log("measuring client-to-node network")
        network["client_to_node"] = client_network(ctx)
        p = ansible("network-matrix.yml", {"out": os.path.join(run_dir, "network-nodes.json"),
                                           **({"iperf": True} if a.iperf else {})}, check=False)
        nn = os.path.join(run_dir, "network-nodes.json")
        if p.returncode == 0 and os.path.exists(nn):
            network.update({k: v for k, v in json.load(open(nn)).items() if k == "pairs"})
            os.remove(nn)
    json.dump(network, open(os.path.join(run_dir, "network.json"), "w"), indent=2)

    if not a.skip_prepare:
        log("preparing dataset")
        p = subprocess.run(jcommon + ["-t", os.path.join(ROOT, "jmeter", "scenarios", "prepare.jmx"),
                                      "-l", os.path.join(run_dir, "prepare.jtl"), "-j", os.path.join(run_dir, "prepare.log")],
                           capture_output=True, text=True)
        if p.returncode != 0 or ",false," in open(os.path.join(run_dir, "prepare.jtl")).read():
            raise SystemExit(f"dataset preparation failed; see {run_dir}/prepare.jtl and prepare.log")

    events = Events(os.path.join(run_dir, "failover-events.json"))
    poller = Poller(ctx, events)
    poller.start()
    time.sleep(1.5)
    deadline = now() + 120
    while not poller.converged() and now() < deadline:
        time.sleep(1)

    primary = (poller.writable() or ctx["writable_nodes"] or [None])[0]
    fault_node = a.fault_node or primary
    meta = {
        "run_id": run_id, "scenario": a.scenario, "ha_provider": ctx["ha_provider"], "environment": ctx["environment"],
        "client_location": ctx["client_location"], "endpoint": ctx["endpoint"], "initial_primary": primary,
        "fault": fault, "fault_node": fault_node if fault != "none" else None, "warmup_s": warmup,
        "jmeter_properties": {k: v for k, v in props.items() if k not in ("lab_script_dir",)},
        "git_commit": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip(),
        "measurement_clock": "client wall clock (UTC) for events; System.nanoTime() for durations",
    }
    json.dump(meta, open(os.path.join(run_dir, "metadata.json"), "w"), indent=2)

    jtl = os.path.join(run_dir, "jmeter.jtl")
    exporter = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "benchmark", "runner", "jtl_exporter.py"),
                                 "--jtl", jtl, "--port", "9464",
                                 "--label", f"environment={ctx['environment']}", "--label", f"ha_provider={ctx['ha_provider']}",
                                 "--label", f"scenario={a.scenario}", "--label", f"endpoint_type={ctx['endpoint']['type']}"])
    started = now()
    meta.update({"started_at": iso(started), "started_at_epoch": started})
    ann = grafana_annotation(run_id, a.scenario, int(started * 1000))
    log(f"starting {a.scenario}: {props.get('threads', 'default')} threads, {duration:.0f}s, endpoint {ctx['endpoint']['type']}")
    jm = subprocess.Popen(jcommon + ["-t", os.path.join(ROOT, "jmeter", "scenarios", f"{a.scenario}.jmx"),
                                     "-l", jtl, "-j", os.path.join(run_dir, "jmeter.log")],
                          stdout=open(os.path.join(run_dir, "jmeter.out"), "w"), stderr=subprocess.STDOUT)

    def stop(*_):
        jm.send_signal(signal.SIGINT)
    signal.signal(signal.SIGINT, stop)

    restored = fault == "none"
    try:
        if fault != "none":
            fault_at = a.fault_at if a.fault_at is not None else max(warmup + 10, duration / 3)
            while jm.poll() is None and now() - started < fault_at:
                time.sleep(0.2)
            poller.fault_node = fault_node
            base = {"node": fault_node, "fault": fault, "confirm": "yes", "netem": a.netem,
                    "max_seconds": int(a.fault_duration + 300)}
            t_inj = now()
            ansible("fault.yml", {**base, "action": "inject"})
            events.add("fault_injected", t_inj, node=fault_node, fault=fault, completed_at=iso(now()))

            promote_after = a.promote_after if a.promote_after is not None else float(
                (bench.get("failover") or {}).get("manual_promotion_delay_seconds", 10))
            manual = not ctx["auto_failover"] and fault in SERVER_FAULTS
            promoted = False
            while jm.poll() is None and now() - t_inj < a.fault_duration:
                if manual and not a.no_promote and not promoted and events.has("primary_unavailable"):
                    det = next(e["ts"] for e in events.items if e["event"] == "primary_unavailable")
                    if now() - det >= promote_after:
                        promoted = True
                        events.add("manual_promotion_started", now())
                        p = ansible("failover.yml", {"failover_mode": "failover",
                                                     "events_file": os.path.join(run_dir, "promotion-events.json"),
                                                     **({"force": True} if fault == "isolate" else {})}, check=False)
                        events.add("manual_promotion_finished", now(), rc=p.returncode)
                time.sleep(0.2)
            t_res = now()
            ansible("fault.yml", {**base, "action": "restore"}, check=False)
            restored = True
            events.add("fault_restored", t_res, completed_at=iso(now()))
            if manual and promoted and fault in SERVER_FAULTS:
                p = ansible("recover.yml", {"node": fault_node}, check=False)
                events.add("old_primary_rejoined" if p.returncode == 0 else "old_primary_rejoin_failed", now())
        jm.wait()
    finally:
        if not restored:
            log("restoring fault after interruption")
            ansible("fault.yml", {"node": fault_node, "fault": fault, "confirm": "yes", "action": "restore"}, check=False)
        if jm.poll() is None:
            jm.wait()

    finished = now()
    meta.update({"finished_at": iso(finished), "finished_at_epoch": finished})
    grafana_annotation(run_id, a.scenario, int(started * 1000), int(finished * 1000), ann)
    time.sleep(12)  # let Prometheus scrape the final client counters
    exporter.terminate()

    log("waiting for replication to converge")
    deadline = now() + 300
    while not poller.converged() and now() < deadline:
        time.sleep(1)
    if fault != "none":
        deadline = now() + 120
        while not events.has("all_nodes_healthy") and now() < deadline:
            time.sleep(1)
    poller.stop_flag.set()
    meta["final_primary"] = (poller.writable() or [primary])[0]
    meta["replication_converged_after_run"] = poller.converged()
    json.dump(meta, open(os.path.join(run_dir, "metadata.json"), "w"), indent=2)
    if not os.path.exists(events.path):
        json.dump([], open(events.path, "w"))

    ansible("consistency.yml", {"run_id": run_id, "out_dir": os.path.join(run_dir, "ledger")}, check=False)
    os.remove(secret)

    import report  # noqa: E402
    r = report.build(run_dir, a.prometheus)
    log(f"report: {os.path.relpath(os.path.join(run_dir, 'report.md'), ROOT)}")
    for k in ("tps", "rps", "error_rate", "latency_p50_s", "latency_p95_s", "latency_p99_s", "rto_s",
              "lost_acknowledged_writes", "consistency_errors"):
        print(f"  {k:26} {r.get(k)}")


if __name__ == "__main__":
    main()
