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
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "metrics", "collectors"))
sys.path.insert(0, os.path.join(ROOT, "benchmark", "report"))
import netprobe  # noqa: E402
import loadhost  # noqa: E402

SCENARIOS = ["baseline", "read-heavy", "write-heavy", "mixed-workload", "spike-test", "soak-test",
             "failover-test", "network-disruption", "wave-test", "rate-test"]
FAULT_ALIASES = {"stop-mysql": "stop-db", "kill-mysql": "kill-db"}   # old names still accepted
FAULTS = ["none", "stop-db", "kill-db", "isolate", "netem", "degrade", "freeze", "slow-disk", "partition-client",
          "clock-skew", "disk-full", *FAULT_ALIASES]
HOST_WIDE = ("clock-skew", "disk-full")
SCENARIO_FAULT = {"failover-test": "stop-db", "network-disruption": "netem"}
SERVER_FAULTS = ("stop-db", "kill-db", "isolate", "freeze", "partition-client")
FORCE_PROMOTE = ("isolate", "freeze", "partition-client")   # the old primary cannot be fenced cleanly
DOWN_POLLS = 3
NETEM_DEFAULT = {"netem": "delay 100ms 20ms loss 1%", "degrade": "delay 300ms 30ms loss 10%"}


def now():
    return time.time()


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="milliseconds")


def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


ANSIBLE_LOG_DIR = None


def ansible(playbook, extra: dict, check=True):
    cmd = ["ansible-playbook", os.path.join(ROOT, "ansible", "playbooks", playbook)]
    cmd += os.environ.get("VAULT_ARGS", "").split()
    if extra:
        cmd += ["-e", json.dumps(extra)]
    env = {**os.environ, "ANSIBLE_CONFIG": os.path.join(ROOT, "ansible.cfg")}
    log("ansible-playbook " + playbook + " " + " ".join(f"{k}={v}" for k, v in extra.items() if k != "confirm"))
    p = subprocess.run(cmd, env=env, cwd=ROOT, capture_output=True, text=True)
    if ANSIBLE_LOG_DIR:
        with open(os.path.join(ANSIBLE_LOG_DIR, "ansible.log"), "a") as fh:
            fh.write(f"===== {playbook} {json.dumps({k: v for k, v in extra.items() if k != 'confirm'})} rc={p.returncode}\n"
                     + p.stdout + p.stderr)
    if check and p.returncode != 0:
        sys.stderr.write(p.stdout[-4000:] + p.stderr[-2000:])
        raise SystemExit(f"{playbook} failed (rc={p.returncode})")
    return p


def ha_settings(ctx):
    """The provider's section of config/ha.yml plus the engine's extra server options, for comparing runs."""
    import yaml
    try:
        ha = yaml.safe_load(open(os.path.join(ROOT, os.environ.get("LAB_CONFIG_DIR", "config"), "ha.yml"))) or {}
    except OSError:
        return {}
    out = dict(ha.get(ctx["ha_provider"].replace("-", "_")) or {})
    if (ha.get(ctx.get("db_engine", "")) or {}).get("config"):
        out["server_config"] = ha[ctx["db_engine"]]["config"]
    return out


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


def db_alive(addr, port, engine, timeout=1.5):
    """True when the server answers at protocol level; a frozen server still completes TCP connects."""
    try:
        with socket.create_connection((addr, int(port)), timeout=timeout) as c:
            c.settimeout(timeout)
            if engine == "postgres":
                c.sendall(b"\x00\x00\x00\x08\x04\xd2\x16\x2f")   # SSLRequest; the server replies S or N
                return c.recv(1) in (b"S", b"N")
            return len(c.recv(4)) == 4   # MySQL/MariaDB greeting
    except OSError:
        return False


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
        # A single slow scrape is not an outage: the primary counts as down after DOWN_POLLS bad polls in a row.
        self.bad_since, self.bad_polls, self.bad_reason = None, 0, ""

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
                # Client view first: a closed DB port is down; a missing scrape alone is a monitoring problem.
                db_open = db_alive(self.ctx["addresses"][fn], self.ctx["db_port"], self.ctx.get("db_engine", "mysql"))
                if m is None and db_open:
                    self.events.add("collector_unreachable", t, node=fn)
                if not db_open or (m is not None and m.get("ha_lab_writable") != 1):
                    self.bad_since = self.bad_since or t
                    self.bad_polls += 1
                    self.bad_reason = "database not answering clients" if not db_open else "not writable"
                    if self.bad_polls >= DOWN_POLLS:
                        self.events.add("primary_unavailable", self.bad_since, node=fn, reason=self.bad_reason)
                else:
                    self.bad_since, self.bad_polls = None, 0
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

    def primary_down(self):
        return self.bad_polls >= DOWN_POLLS

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
    ap.add_argument("--netem", help='default: "delay 100ms 20ms loss 1%%" for netem, "delay 300ms 30ms loss 10%%" for degrade')
    ap.add_argument("--promote-after", type=float, default=None,
                    help="async/semi-sync: seconds after detection to run manual promotion (default from config, 10)")
    ap.add_argument("--no-promote", action="store_true", help="async/semi-sync: never promote; measure the outage")
    ap.add_argument("--confirm", default="", help="'yes' is required for faults")
    ap.add_argument("--host-wide", action="store_true", help="allow clock-skew and disk-full, which change the whole host")
    ap.add_argument("--warmup", type=float, default=None)
    ap.add_argument("--skip-prepare", action="store_true")
    ap.add_argument("--ui", action="store_true", help="open the scenario in the JMeter GUI with the lab properties; no run is recorded")
    ap.add_argument("--skip-network", action="store_true")
    ap.add_argument("--iperf", action="store_true")
    ap.add_argument("--prometheus", default=os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090"))
    a = ap.parse_args()

    fault = a.fault or SCENARIO_FAULT.get(a.scenario, "none")
    fault = FAULT_ALIASES.get(fault, fault)
    if fault in HOST_WIDE and not a.host_wide:
        raise SystemExit(f"'{fault}' changes the whole host (every service on it). Re-run with --host-wide (make ... HOST_WIDE=1).")
    if fault != "none" and a.confirm != "yes":
        raise SystemExit(f"Fault '{fault}' disrupts the cluster. Re-run with --confirm yes (make ... CONFIRM=yes).")

    jmeter = os.path.join(ROOT, "local", "tools", "jmeter", "bin", "jmeter")
    if not os.path.exists(jmeter):
        raise SystemExit("JMeter missing: run make jmeter-install")

    results = os.path.join(ROOT, "benchmark", "results")
    run_id = new_run_id(results)
    run_dir = os.path.join(results, run_id)
    os.makedirs(os.path.join(run_dir, "ledger"))
    global ANSIBLE_LOG_DIR
    ANSIBLE_LOG_DIR = run_dir
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

    if a.ui:
        if not a.skip_prepare:
            subprocess.run(jcommon + ["-t", os.path.join(ROOT, "jmeter", "scenarios", "prepare.jmx"),
                                      "-l", os.path.join(run_dir, "prepare.jtl")], stdout=subprocess.DEVNULL)
        log("JMeter GUI: the .jmx files are generated by jmeter/build-scenarios.py; use Save As for your own copy")
        try:
            subprocess.run([x for x in jcommon if x != "-n"] + ["-t", os.path.join(ROOT, "jmeter", "scenarios", f"{a.scenario}.jmx")])
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)
        return

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

    lhost = bench.get("load_host")
    if lhost:
        if bench.get("load_host_key"):
            loadhost.SSH += ["-i", os.path.expanduser(bench["load_host_key"])]
        lbase = loadhost.base(lhost)
        log(f"load generator: {lhost} ({lbase})")
        loadhost.sync(lhost, ROOT, lbase)
        loadhost.put_secret(lhost, secret, lbase)

    if not a.skip_prepare:
        log("preparing dataset")
        pcmd = jcommon + ["-t", os.path.join(ROOT, "jmeter", "scenarios", "prepare.jmx"),
                          "-l", os.path.join(run_dir, "prepare.jtl"), "-j", os.path.join(run_dir, "prepare.log")]
        if lhost:
            pp = loadhost.run(lhost, pcmd, ROOT, run_dir, lbase, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pp.wait()
            loadhost.fetch(lhost, f"{lbase}/run/prepare.jtl", os.path.join(run_dir, "prepare.jtl"))
            p = subprocess.CompletedProcess(pcmd, pp.returncode)
        else:
            p = subprocess.run(pcmd, capture_output=True, text=True)
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
        "ha_settings": ha_settings(ctx),
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
    jcmd = jcommon + ["-t", os.path.join(ROOT, "jmeter", "scenarios", f"{a.scenario}.jmx"),
                      "-l", jtl, "-j", os.path.join(run_dir, "jmeter.log")]
    jout = open(os.path.join(run_dir, "jmeter.out"), "w")
    follower = None
    if lhost:
        follower = loadhost.follow(lhost, f"{lbase}/run/jmeter.jtl", jtl)
        jm = loadhost.run(lhost, jcmd, ROOT, run_dir, lbase, stdout=jout, stderr=subprocess.STDOUT)
    else:
        jm = subprocess.Popen(jcmd, stdout=jout, stderr=subprocess.STDOUT)

    def stop(*_):
        loadhost.stop(lhost) if lhost else jm.send_signal(signal.SIGINT)
    signal.signal(signal.SIGINT, stop)

    restored = fault == "none"
    try:
        if fault != "none":
            fault_at = a.fault_at if a.fault_at is not None else max(warmup + 10, duration / 3)
            while jm.poll() is None and now() - started < fault_at:
                time.sleep(0.2)
            poller.fault_node = fault_node
            base = {"node": fault_node, "fault": fault, "confirm": "yes", "netem": a.netem or NETEM_DEFAULT.get(fault, NETEM_DEFAULT["netem"]),
                    "max_seconds": int(a.fault_duration + 300), **({"host_wide": "yes"} if a.host_wide else {})}
            t_inj = now()
            ansible("fault.yml", {**base, "action": "inject"})
            events.add("fault_injected", t_inj, node=fault_node, fault=fault, completed_at=iso(now()))

            promote_after = a.promote_after if a.promote_after is not None else float(
                (bench.get("failover") or {}).get("manual_promotion_delay_seconds", 10))
            manual = not ctx["auto_failover"] and fault in SERVER_FAULTS
            promoted = False
            while jm.poll() is None and now() - t_inj < a.fault_duration:
                if manual and not a.no_promote and not promoted and events.has("primary_unavailable"):
                    if not poller.primary_down():
                        events.add("primary_recovered_before_promotion", now(), repeat=True)
                        events.items = [e for e in events.items if e["event"] != "primary_unavailable"]
                        continue
                    det = next(e["ts"] for e in events.items if e["event"] == "primary_unavailable")
                    if now() - det >= promote_after:
                        promoted = True
                        events.add("manual_promotion_started", now())
                        p = ansible("failover.yml", {"failover_mode": "failover",
                                                     "events_file": os.path.join(run_dir, "promotion-events.json"),
                                                     **({"force": True} if fault in FORCE_PROMOTE else {})}, check=False)
                        events.add("manual_promotion_finished", now(), rc=p.returncode)
                time.sleep(0.2)
            t_res = now()
            record = os.path.join(run_dir, "fault-check.json")
            ansible("fault.yml", {**base, "action": "restore", "fault_record": record,
                                  **({"fence": True} if promoted else {})}, check=False)
            try:
                fc = json.load(open(record))
                if fc.get("active_since_epoch"):
                    events.add("fault_active", fc["active_since_epoch"], node=fault_node)
                events.add("fault_check", now(), active_at_restore=fc.get("active_at_restore"))
            except (OSError, ValueError):
                events.add("fault_check", now(), active_at_restore=None)
            restored = True
            events.add("fault_restored", t_res, completed_at=iso(now()))
            if manual and promoted and fault in SERVER_FAULTS:
                p = ansible("recover.yml", {"node": fault_node, "rebuild": True}, check=False)
                events.add("old_primary_rejoined" if p.returncode == 0 else "old_primary_rejoin_failed", now())
        jm.wait()
    finally:
        if not restored:
            log("restoring fault after interruption")
            ansible("fault.yml", {"node": fault_node, "fault": fault, "confirm": "yes", "action": "restore"}, check=False)
        if jm.poll() is None:
            jm.wait()

    if follower:
        time.sleep(3)
        follower.terminate()
        loadhost.fetch(lhost, f"{lbase}/run/jmeter.log", os.path.join(run_dir, "jmeter.log"))
        loadhost.cleanup(lhost, lbase)
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
