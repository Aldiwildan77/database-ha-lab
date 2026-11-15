#!/usr/bin/env python3
"""Node-to-node network probe. Run periodically (systemd timer); writes a node_exporter textfile.

Config /etc/ha-lab/network-probe.json:
  {"self": "mysql-01", "port": 3306, "peers": [{"name": "mysql-02", "address": "100.x", "datacenter": "dc-02"}],
   "textfile": "/var/lib/node_exporter/textfile/ha_lab_network.prom", "state": "/var/lib/ha-lab/network-state.json"}
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import netprobe  # noqa: E402
import promlib  # noqa: E402

RTT_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.5, 1)


def main():
    cfg = json.load(open(os.environ.get("HA_LAB_PROBE_CONFIG", "/etc/ha-lab/network-probe.json")))
    state_path = cfg["state"]
    try:
        state = json.load(open(state_path))
    except (OSError, ValueError):
        state = {}
    rtt = promlib.Histogram("network_rtt_seconds", "ICMP round-trip time to peer.", RTT_BUCKETS)
    tcp = promlib.Histogram("tcp_connect_duration_seconds", "TCP handshake time to peer MySQL port.", RTT_BUCKETS)
    sent = promlib.Counter("network_probe_packets_sent_total", "ICMP probes sent.")
    lost = promlib.Counter("network_probe_packets_lost_total", "ICMP probes without reply.")
    rtt.load(state.get("rtt"))
    tcp.load(state.get("tcp"))
    for k, v in (state.get("sent") or {}).items():
        sent._data[tuple(tuple(p.split("=", 1)) for p in k.split("|"))] = v
    for k, v in (state.get("lost") or {}).items():
        lost._data[tuple(tuple(p.split("=", 1)) for p in k.split("|"))] = v

    reg = promlib.Registry()
    for p in cfg["peers"]:
        lbl = {"peer": p["name"], "peer_datacenter": p.get("datacenter", ""), "peer_network": p.get("network", "")}
        rtts, n, ok = netprobe.ping(p["address"])
        for r in rtts:
            rtt.observe(r, **lbl)
        sent.inc(n, **lbl)
        lost.inc(n - ok, **lbl)
        reg.gauge("network_packet_loss_ratio", (n - ok) / n if n else 1, lbl, "Loss ratio of the last probe round.")
        if rtts:
            reg.gauge("network_rtt_last_seconds", sum(rtts) / len(rtts), lbl, "Mean RTT of the last probe round.")
        both_ts = cfg.get("self_network_type", "tailscale") == "tailscale" and p.get("network_type", "tailscale") == "tailscale"
        direct, ts_rtt, _ = netprobe.tailscale_ping(p["address"]) if both_ts else (None, None, "")
        if direct is not None:
            reg.gauge("network_tailscale_direct", 1 if direct else 0, lbl, "1 = direct WireGuard path, 0 = DERP relay.")
            reg.gauge("network_tailscale_rtt_seconds", ts_rtt, lbl)
        for _ in range(3):
            d = netprobe.tcp_connect(p["address"], int(cfg.get("port", 3306)))
            if d is not None:
                tcp.observe(d, **lbl)
        reg.gauge("network_probe_success", 1 if ok else 0, lbl)
    for m in (rtt, tcp, sent, lost):
        m.export(reg)
    reg.gauge("network_probe_timestamp_seconds", time.time())

    tmp = cfg["textfile"] + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(reg.render())
    os.replace(tmp, cfg["textfile"])
    with open(state_path + ".tmp", "w") as fh:
        json.dump({"rtt": rtt.state(), "tcp": tcp.state(),
                   "sent": {"|".join(f"{a}={b}" for a, b in k): v for k, v in sent._data.items()},
                   "lost": {"|".join(f"{a}={b}" for a, b in k): v for k, v in lost._data.items()}}, fh)
    os.replace(state_path + ".tmp", state_path)


if __name__ == "__main__":
    main()
