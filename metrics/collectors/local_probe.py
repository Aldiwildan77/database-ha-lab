#!/usr/bin/env python3
"""Local (client machine) to node network probe exporter. Prometheus scrapes it on host.docker.internal.

Usage: local_probe.py --targets local/prometheus/targets/db.json --port 9465 --mysql-port 3306
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import netprobe  # noqa: E402
import promlib  # noqa: E402

BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.5, 1, 2.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True)
    ap.add_argument("--port", type=int, default=9465)
    ap.add_argument("--mysql-port", type=int, default=3306)
    ap.add_argument("--interval", type=float, default=5)
    a = ap.parse_args()

    rtt = promlib.Histogram("network_rtt_seconds", "ICMP RTT from the benchmark client to the node.", BUCKETS)
    tcp = promlib.Histogram("tcp_connect_duration_seconds", "TCP handshake from the benchmark client to MySQL.", BUCKETS)
    sent = promlib.Counter("network_probe_packets_sent_total", "ICMP probes sent.")
    lost = promlib.Counter("network_probe_packets_lost_total", "ICMP probes without reply.")
    tcp_fail = promlib.Counter("connection_errors_total", "Failed TCP connects to MySQL from the client probe.")
    gauges: dict = {}
    lock = threading.Lock()

    def nodes():
        out = []
        for t in json.load(open(a.targets)):
            host = t["targets"][0].rsplit(":", 1)[0].strip("[]")
            out.append((t["labels"]["node"], host, t["labels"].get("datacenter", ""),
                        t["labels"].get("network_type", "tailscale")))
        return out

    def loop():
        n = 0
        while True:
            for name, host, dc, ntype in nodes():
                lbl = {"peer": name, "peer_datacenter": dc}
                rtts, s, ok = netprobe.ping(host, count=3)
                for r in rtts:
                    rtt.observe(r, **lbl)
                sent.inc(s, **lbl)
                lost.inc(s - ok, **lbl)
                d = netprobe.tcp_connect(host, a.mysql_port)
                if d is None:
                    tcp_fail.inc(**lbl)
                else:
                    tcp.observe(d, **lbl)
                with lock:
                    gauges[("network_packet_loss_ratio", name, dc)] = (s - ok) / s
                    gauges[("network_probe_success", name, dc)] = 1 if ok else 0
                if n % 6 == 0 and ntype == "tailscale":
                    direct, ts_rtt, _ = netprobe.tailscale_ping(host)
                    with lock:
                        if direct is not None:
                            gauges[("network_tailscale_direct", name, dc)] = 1 if direct else 0
                            gauges[("network_tailscale_rtt_seconds", name, dc)] = ts_rtt
            n += 1
            time.sleep(a.interval)

    threading.Thread(target=loop, daemon=True).start()

    def collect(reg):
        for m in (rtt, tcp, sent, lost, tcp_fail):
            m.export(reg)
        with lock:
            for (metric, peer, dc), v in gauges.items():
                reg.gauge(metric, v, {"peer": peer, "peer_datacenter": dc})

    promlib.serve(a.port, collect)


if __name__ == "__main__":
    main()
