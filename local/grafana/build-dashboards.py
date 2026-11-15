#!/usr/bin/env python3
"""Generates local/grafana/dashboards/*.json. Edit this file, then run: make dashboards"""
import json, os, sys
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboards")
DS = {"type": "prometheus", "uid": "prometheus"}

def var(name, query, label, include_all=True):
    return {"name": name, "label": label, "type": "query", "datasource": DS,
            "query": {"query": query, "refId": name}, "definition": query,
            "refresh": 2, "includeAll": include_all, "multi": True, "allValue": ".*",
            "current": {"text": "All", "value": "$__all"}, "sort": 1}

def tvars(metric):
    return [var("environment", f"label_values({metric}, environment)", "Environment"),
            var("datacenter", f'label_values({metric}{{environment=~"$environment"}}, datacenter)', "Datacenter"),
            var("node", f'label_values({metric}{{environment=~"$environment",datacenter=~"$datacenter"}}, node)', "Node")]

SEL = 'environment=~"$environment",datacenter=~"$datacenter",node=~"$node"'

class D:
    def __init__(s): s.panels, s.id, s.y = [], 1, 0; s.x = 0
    def row(s, title):
        s.newline(); s.panels.append({"type": "row", "title": title, "id": s.id, "collapsed": False,
            "gridPos": {"x": 0, "y": s.y, "w": 24, "h": 1}, "panels": []}); s.id += 1; s.y += 1
    def newline(s):
        if s.x: s.x = 0; s.y += s.h
    def add(s, ptype, title, targets, unit="short", w=12, h=8, desc=None, extra=None):
        if s.x + w > 24: s.newline()
        p = {"type": ptype, "title": title, "id": s.id, "datasource": DS,
             "gridPos": {"x": s.x, "y": s.y, "w": w, "h": h},
             "targets": [{"refId": chr(65+i), "datasource": DS, "expr": e, "legendFormat": l}
                         for i, (e, l) in enumerate(targets)],
             "fieldConfig": {"defaults": {"unit": unit}, "overrides": []}, "options": {}}
        if desc: p["description"] = desc
        if ptype == "timeseries":
            p["fieldConfig"]["defaults"]["custom"] = {"lineWidth": 1, "fillOpacity": 10, "showPoints": "never"}
            p["options"] = {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max", "lastNotNull"]},
                            "tooltip": {"mode": "multi", "sort": "desc"}}
        if ptype == "stat":
            p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "textMode": "value_and_name"}
        if extra: 
            for k, v in extra.items(): p[k] = v
        s.panels.append(p); s.id += 1; s.x += w; s.h = h
    def text(s, title, content, w=24, h=4):
        if s.x + w > 24: s.newline()
        s.panels.append({"type": "text", "title": title, "id": s.id, "gridPos": {"x": s.x, "y": s.y, "w": w, "h": h},
                         "options": {"mode": "markdown", "content": content}})
        s.id += 1; s.x += w; s.h = h
    def save(s, path, uid, title, tags, metric, templating=None):
        d = {"uid": uid, "title": title, "tags": ["database-ha-lab"] + tags, "timezone": "browser",
             "schemaVersion": 39, "version": 1, "editable": False, "refresh": "30s",
             "time": {"from": "now-1h", "to": "now"}, "graphTooltip": 1,
             "templating": {"list": templating if templating is not None else tvars(metric)},
             "annotations": {"list": [{"name": "Benchmark runs", "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                                       "enable": True, "iconColor": "rgba(255, 152, 48, 0.8)",
                                       "target": {"type": "tags", "tags": ["benchmark"], "limit": 100, "matchAny": True}}]},
             "panels": s.panels}
        json.dump(d, open(path, "w"), indent=2); open(path, "a").write("\n")

UPMAP = {"mappings": [{"type": "value", "options": {"0": {"text": "DOWN", "color": "red"}, "1": {"text": "UP", "color": "green"}}}],
         "thresholds": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}]}}

# ---------------- MySQL overview
m = D()
m.row("Availability")
m.add("stat", "Database up", [(f"mysql_up{{{SEL}}}", "{{node}}")], w=8, h=4, extra={"fieldConfig": {"defaults": {"unit": "none", **UPMAP}, "overrides": []}})
m.add("stat", "Uptime", [(f"mysql_global_status_uptime{{{SEL}}}", "{{node}}")], unit="s", w=8, h=4)
m.add("stat", "Exporter scrape up", [(f'up{{job="db",{SEL}}}', "{{node}}")], w=8, h=4, extra={"fieldConfig": {"defaults": {"unit": "none", **UPMAP}, "overrides": []}})
m.row("Throughput")
m.add("timeseries", "Queries / s", [(f"rate(mysql_global_status_queries{{{SEL}}}[$__rate_interval])", "{{node}}")], unit="qps")
m.add("timeseries", "Transactions / s (commit + rollback)", [(f'sum by (node) (rate(mysql_global_status_commands_total{{command=~"commit|rollback",{SEL}}}[$__rate_interval]))', "{{node}}")], unit="ops",
      desc="Explicit COMMIT/ROLLBACK statements. Autocommit statements are not counted here.")
m.add("timeseries", "Commands / s", [(f'sum by (node, command) (rate(mysql_global_status_commands_total{{command=~"select|insert|update|delete|replace",{SEL}}}[$__rate_interval]))', "{{node}} {{command}}")], unit="ops")
m.add("timeseries", "Slow queries / s", [(f"rate(mysql_global_status_slow_queries{{{SEL}}}[$__rate_interval])", "{{node}}")], unit="ops", desc="Statements slower than long_query_time (1s by default in this lab).")
m.row("Connections")
m.add("timeseries", "Threads connected / running", [(f"mysql_global_status_threads_connected{{{SEL}}}", "{{node}} connected"), (f"mysql_global_status_threads_running{{{SEL}}}", "{{node}} running")])
m.add("timeseries", "Connection usage", [(f"mysql_global_status_threads_connected{{{SEL}}} / mysql_global_variables_max_connections{{{SEL}}}", "{{node}}")], unit="percentunit")
m.add("timeseries", "Aborted connects / s", [(f"rate(mysql_global_status_aborted_connects{{{SEL}}}[$__rate_interval])", "{{node}}")], unit="ops")
m.add("timeseries", "Network traffic", [(f"rate(mysql_global_status_bytes_received{{{SEL}}}[$__rate_interval])", "{{node}} rx"), (f"rate(mysql_global_status_bytes_sent{{{SEL}}}[$__rate_interval])", "{{node}} tx")], unit="Bps")
m.row("InnoDB")
m.add("timeseries", "Buffer pool hit ratio", [(f"1 - rate(mysql_global_status_innodb_buffer_pool_reads{{{SEL}}}[$__rate_interval]) / rate(mysql_global_status_innodb_buffer_pool_read_requests{{{SEL}}}[$__rate_interval])", "{{node}}")], unit="percentunit")
m.add("timeseries", "Buffer pool pages", [(f"mysql_global_status_buffer_pool_pages{{{SEL}}}", "{{node}} {{state}}")])
m.add("timeseries", "Row lock waits / s", [(f"rate(mysql_global_status_innodb_row_lock_waits{{{SEL}}}[$__rate_interval])", "{{node}}")], unit="ops")
m.add("timeseries", "Avg row lock wait time", [(f"rate(mysql_global_status_innodb_row_lock_time{{{SEL}}}[$__rate_interval]) / 1000 / rate(mysql_global_status_innodb_row_lock_waits{{{SEL}}}[$__rate_interval])", "{{node}}")], unit="s",
      desc="Innodb_row_lock_time is in milliseconds; converted to seconds.")
m.add("timeseries", "Deadlocks / s", [(f"rate(mysql_info_schema_innodb_metrics_lock_lock_deadlocks_total{{{SEL}}}[$__rate_interval])", "{{node}}")], unit="ops",
      desc="From INFORMATION_SCHEMA.INNODB_METRICS lock_deadlocks (enabled by default).")
m.add("timeseries", "Data fsyncs / s", [(f"rate(mysql_global_status_innodb_data_fsyncs{{{SEL}}}[$__rate_interval])", "{{node}}")], unit="ops")
m.add("timeseries", "Redo log written", [(f"rate(mysql_global_status_innodb_os_log_written{{{SEL}}}[$__rate_interval])", "{{node}}")], unit="Bps")
m.add("timeseries", "Row operations / s", [(f"rate(mysql_global_status_innodb_row_ops_total{{{SEL}}}[$__rate_interval])", "{{node}} {{operation}}")], unit="ops")
m.row("Replication")
m.add("timeseries", "Replication lag (Seconds_Behind_Source)",
      [(f"mysql_slave_status_seconds_behind_source{{{SEL}}} or mysql_slave_status_seconds_behind_master{{{SEL}}}", "{{node}}")], unit="s",
      desc="Reported by SHOW REPLICA STATUS. Empty until replication is configured (make ha). Coarse 1s resolution; NULL when the SQL thread is stopped.")
m.add("timeseries", "Binlog size", [(f"mysql_binlog_size_bytes{{{SEL}}}", "{{node}}")], unit="bytes")
m.add("timeseries", "Server statement latency (Performance Schema)", [(f'histogram_quantile({q}, sum by (node, le) (rate(database_statement_duration_seconds_bucket{{side="server",{SEL}}}[$__rate_interval])))', "{{node}} p" + str(int(q*100))) for q in (0.5, 0.99)], unit="s")
m.add("timeseries", "Server commit latency (Performance Schema)", [(f'histogram_quantile({q}, sum by (node, le) (rate(commit_duration_seconds_bucket{{side="server",{SEL}}}[$__rate_interval])))', "{{node}} p" + str(int(q*100))) for q in (0.5, 0.99)], unit="s",
      desc="Explicit COMMIT statements. Includes redo/binlog flush and semi-sync, Group Replication or Galera certification waits.")
m.save(f"{out}/mysql-overview.json", "lab-mysql-overview", "MySQL Performance", ["mysql"], "mysql_up")

# ---------------- Host overview
h = D()
NS = f'job="node",{SEL}'
h.row("Summary")
h.add("stat", "Node exporter up", [(f"up{{{NS}}}", "{{node}}")], w=6, h=4, extra={"fieldConfig": {"defaults": {"unit": "none", **UPMAP}, "overrides": []}})
h.add("stat", "Clock synchronised", [(f"node_timex_sync_status{{{NS}}}", "{{node}}")], w=6, h=4, extra={"fieldConfig": {"defaults": {"unit": "none", "mappings": [{"type": "value", "options": {"0": {"text": "NO", "color": "red"}, "1": {"text": "YES", "color": "green"}}}]}, "overrides": []}})
h.add("stat", "Clock offset", [(f"node_timex_offset_seconds{{{NS}}}", "{{node}}")], unit="s", w=6, h=4, desc="Kernel-estimated offset from the NTP reference. Cross-node timestamps carry at least this uncertainty.")
h.add("stat", "Uptime", [(f"time() - node_boot_time_seconds{{{NS}}}", "{{node}}")], unit="s", w=6, h=4)
h.row("CPU & memory")
h.add("timeseries", "CPU utilisation", [(f'1 - avg by (node) (rate(node_cpu_seconds_total{{mode="idle",{NS}}}[$__rate_interval]))', "{{node}}")], unit="percentunit")
h.add("timeseries", "CPU by mode", [(f'sum by (node, mode) (rate(node_cpu_seconds_total{{mode!="idle",{NS}}}[$__rate_interval])) / on (node) group_left count by (node) (node_cpu_seconds_total{{mode="idle",{NS}}})', "{{node}} {{mode}}")], unit="percentunit",
      desc="iowait is time the CPU was idle with outstanding I/O, not CPU work.")
h.add("timeseries", "Load average (1m) per CPU", [(f'node_load1{{{NS}}} / on (node) count by (node) (node_cpu_seconds_total{{mode="idle",{NS}}})', "{{node}}")])
h.add("timeseries", "Memory utilisation", [(f"1 - node_memory_MemAvailable_bytes{{{NS}}} / node_memory_MemTotal_bytes{{{NS}}}", "{{node}}")], unit="percentunit")
h.add("timeseries", "Swap used", [(f"node_memory_SwapTotal_bytes{{{NS}}} - node_memory_SwapFree_bytes{{{NS}}}", "{{node}}")], unit="bytes")
h.add("timeseries", "Pressure (PSI) waiting share", [(f"rate(node_pressure_cpu_waiting_seconds_total{{{NS}}}[$__rate_interval])", "{{node}} cpu"), (f"rate(node_pressure_io_waiting_seconds_total{{{NS}}}[$__rate_interval])", "{{node}} io"), (f"rate(node_pressure_memory_waiting_seconds_total{{{NS}}}[$__rate_interval])", "{{node}} memory")], unit="percentunit")
DK = f'{NS},device!~"loop.*|ram.*|sr.*"'
h.row("Storage")
h.add("timeseries", "Disk utilisation (busy time)", [(f"rate(node_disk_io_time_seconds_total{{{DK}}}[$__rate_interval])", "{{node}} {{device}}")], unit="percentunit",
      desc="Share of time the device had I/O in flight. Saturation signal for HDDs; NVMe/SSD can exceed real capacity well before 100%.")
h.add("timeseries", "IOPS", [(f"rate(node_disk_reads_completed_total{{{DK}}}[$__rate_interval])", "{{node}} {{device}} read"), (f"rate(node_disk_writes_completed_total{{{DK}}}[$__rate_interval])", "{{node}} {{device}} write")], unit="iops")
h.add("timeseries", "Throughput", [(f"rate(node_disk_read_bytes_total{{{DK}}}[$__rate_interval])", "{{node}} {{device}} read"), (f"rate(node_disk_written_bytes_total{{{DK}}}[$__rate_interval])", "{{node}} {{device}} write")], unit="Bps")
h.add("timeseries", "Average I/O latency", [(f"rate(node_disk_read_time_seconds_total{{{DK}}}[$__rate_interval]) / rate(node_disk_reads_completed_total{{{DK}}}[$__rate_interval])", "{{node}} {{device}} read"), (f"rate(node_disk_write_time_seconds_total{{{DK}}}[$__rate_interval]) / rate(node_disk_writes_completed_total{{{DK}}}[$__rate_interval])", "{{node}} {{device}} write")], unit="s",
      desc="Derived mean (time / operations) from /proc/diskstats, not a distribution.")
h.add("timeseries", "Filesystem used", [(f'1 - node_filesystem_avail_bytes{{fstype!~"tmpfs|overlay|squashfs",{NS}}} / node_filesystem_size_bytes{{fstype!~"tmpfs|overlay|squashfs",{NS}}}', "{{node}} {{mountpoint}}")], unit="percentunit")
NI = f'{NS},device!~"lo|docker.*|veth.*"'
h.row("Network")
h.add("timeseries", "Network receive", [(f"rate(node_network_receive_bytes_total{{{NI}}}[$__rate_interval])", "{{node}} {{device}}")], unit="Bps")
h.add("timeseries", "Network transmit", [(f"rate(node_network_transmit_bytes_total{{{NI}}}[$__rate_interval])", "{{node}} {{device}}")], unit="Bps")
h.add("timeseries", "TCP retransmitted segments / s", [(f"rate(node_netstat_Tcp_RetransSegs{{{NS}}}[$__rate_interval])", "{{node}}")], unit="ops")
h.add("timeseries", "Packet errors & drops / s", [(f"rate(node_network_receive_errs_total{{{NI}}}[$__rate_interval]) + rate(node_network_transmit_errs_total{{{NI}}}[$__rate_interval])", "{{node}} {{device}} errors"), (f"rate(node_network_receive_drop_total{{{NI}}}[$__rate_interval]) + rate(node_network_transmit_drop_total{{{NI}}}[$__rate_interval])", "{{node}} {{device}} drops")], unit="pps")
h.save(f"{out}/host-overview.json", "lab-host-overview", "Host & Storage Overview", ["host"], "node_uname_info")

# shared helpers for the remaining dashboards
def q(metric_rate, quant, by="le"):
    return f"histogram_quantile({quant}, sum by ({by}) (rate({metric_rate}[$__rate_interval])))"

BV = 'ha_provider=~"$ha_provider",scenario=~"$scenario"'
bench_vars = [var("ha_provider", "label_values(ha_transaction_total, ha_provider)", "HA provider"),
              var("scenario", 'label_values(ha_transaction_total{ha_provider=~"$ha_provider"}, scenario)', "Scenario")]
node_vars = tvars("ha_lab_db_up")
STATE = {"mappings": [{"type": "value", "options": {"0": {"text": "no", "color": "red"}, "1": {"text": "yes", "color": "green"}}}],
         "thresholds": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}]}}
NSEL = SEL

# ---------------- 1. HA overview
o = D()
o.row("Client view (benchmark)")
o.add("stat", "TPS (committed)", [(f"sum(rate(ha_transaction_success_total{{{BV}}}[1m]))", "TPS")], unit="ops", w=4, h=4)
o.add("stat", "RPS (attempted)", [(f"sum(rate(ha_transaction_total{{{BV}}}[1m]))", "RPS")], unit="reqps", w=4, h=4)
o.add("stat", "Error rate", [(f"sum(rate(ha_transaction_errors_total{{{BV}}}[1m])) / sum(rate(ha_transaction_total{{{BV}}}[1m]))", "errors")], unit="percentunit", w=4, h=4)
o.add("stat", "Write availability", [(f'sum(rate(ha_transaction_success_total{{operation="write",{BV}}}[1m])) / sum(rate(ha_transaction_total{{operation="write",{BV}}}[1m]))', "writes")], unit="percentunit", w=4, h=4)
o.add("stat", "Read availability", [(f'sum(rate(ha_transaction_success_total{{operation="read",{BV}}}[1m])) / sum(rate(ha_transaction_total{{operation="read",{BV}}}[1m]))', "reads")], unit="percentunit", w=4, h=4)
o.add("stat", "Current primary / writable nodes", [('ha_lab_writable == 1', "{{node}}")], w=4, h=4,
      extra={"options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "textMode": "name", "colorMode": "background"}})
o.add("timeseries", "Transaction latency (end-to-end, client)", [(q(f'ha_transaction_duration_seconds_bucket{{outcome="success",{BV}}}', x, "le"), f"p{int(x*100)}") for x in (0.5, 0.95, 0.99)], unit="s")
o.add("timeseries", "Throughput", [(f"sum by (operation) (rate(ha_transaction_success_total{{{BV}}}[$__rate_interval]))", "{{operation}} ok"), (f"sum by (error_class) (rate(ha_transaction_errors_total{{{BV}}}[$__rate_interval]))", "error {{error_class}}")], unit="ops")
o.row("Cluster")
o.add("stat", "Database up", [("ha_lab_db_up", "{{node}}")], w=8, h=4, extra={"fieldConfig": {"defaults": {"unit": "none", **UPMAP}, "overrides": []}})
o.add("stat", "Healthy replicas / members", [("ha_lab_replica_io_running * ha_lab_replica_sql_running or ha_lab_group_member_online or ha_lab_wsrep_ready", "{{node}}")], w=8, h=4, extra={"fieldConfig": {"defaults": {"unit": "none", **STATE}, "overrides": []}})
o.add("stat", "Max replication lag", [("max(replication_lag_seconds)", "lag")], unit="s", w=8, h=4)
o.add("timeseries", "Replication lag", [("replication_lag_seconds", "{{node}} applier"), ("mysql_slave_status_seconds_behind_source or mysql_slave_status_seconds_behind_master", "{{node}} Seconds_Behind_Source")], unit="s")
o.add("timeseries", "CPU / memory utilisation", [("lab:node_cpu_utilization:ratio", "{{node}} cpu"), ("lab:node_memory_utilization:ratio", "{{node}} mem")], unit="percentunit")
o.save(f"{out}/ha-overview.json", "lab-ha-overview", "HA Overview", ["ha"], "", bench_vars)

# ---------------- 2. Latency breakdown
lb = D()
lb.text("How to read this", "Durations below are measured **independently** and **overlap**; they do not add up to the "
        "end-to-end time. Client panels come from the JMeter sampler (`System.nanoTime()`); server panels from Performance "
        "Schema; storage from Performance Schema file I/O and `/proc/diskstats` (means, not distributions); network RTT from "
        "ICMP probes. Commit (server) already contains redo/binlog fsync and any replication acknowledgement wait.", h=3)
lb.row("End-to-end (client)")
for title, metric, extra in (("End-to-end transaction", "ha_transaction_duration_seconds_bucket", 'outcome="success",'),
                             ("BEGIN..COMMIT (excl. connect)", "transaction_duration_seconds_bucket", 'side="client",'),
                             ("Connection establishment (reconnects)", "connection_duration_seconds_bucket", "")):
    lb.add("timeseries", title, [(q(f"{metric}{{{extra}{BV}}}", x), f"p{int(x*100)}") for x in (0.5, 0.95, 0.99)], unit="s", w=8)
lb.row("Database: client vs server")
lb.add("timeseries", "Statement time: client (incl. network) vs server", [(q(f'database_statement_duration_seconds_bucket{{side="client",{BV}}}', x), f"client p{int(x*100)}") for x in (0.5, 0.99)] + [(q('database_statement_duration_seconds_bucket{side="server"}', x, "le"), f"server p{int(x*100)}") for x in (0.5, 0.99)], unit="s",
       desc="Client value covers all statements of one transaction; server value is per statement across all sessions.")
lb.add("timeseries", "Commit: client vs server", [(q(f'commit_duration_seconds_bucket{{side="client",operation="write",{BV}}}', x), f"client p{int(x*100)}") for x in (0.5, 0.99)] + [(q('commit_duration_seconds_bucket{side="server"}', x, "node, le"), "{{node}} server p" + str(int(x*100))) for x in (0.5, 0.99)], unit="s")
lb.row("Replication, storage, network")
lb.add("timeseries", "Replication apply (sampled)", [(q("replication_apply_duration_seconds_bucket", x, "node, le"), "{{node}} p" + str(int(x*100))) for x in (0.5, 0.99)], unit="s", w=8)
lb.add("timeseries", "fsync mean (Performance Schema)", [("rate(storage_fsync_seconds_total[$__rate_interval]) / rate(storage_fsync_total[$__rate_interval])", "{{node}} {{file}}")], unit="s", w=8,
       desc="Mean time of file 'misc' operations (fsync and similar) per file class. Derived mean.")
lb.add("timeseries", "Disk write latency (mean)", [('rate(node_disk_write_time_seconds_total{device!~"loop.*|ram.*|sr.*|dm-.*"}[$__rate_interval]) / rate(node_disk_writes_completed_total{device!~"loop.*|ram.*|sr.*|dm-.*"}[$__rate_interval])', "{{node}} {{device}}")], unit="s", w=8)
lb.add("timeseries", "Network RTT client → node (p50)", [(q('network_rtt_seconds_bucket{job="local_probe"}', 0.5, "peer, le"), "{{peer}}")], unit="s")
lb.add("timeseries", "Lock wait time per second", [("rate(database_lock_wait_seconds_total[$__rate_interval])", "{{node}}")], unit="s",
       desc="InnoDB row lock wait seconds accumulated per second (millisecond resolution). Mean only.")
lb.save(f"{out}/latency-breakdown.json", "lab-latency-breakdown", "Latency Breakdown", ["latency"], "", bench_vars)

# ---------------- 3. Network
n = D()
n.row("Node to node")
n.add("table", "RTT matrix p50 (row = source, column = destination)",
      [(q('network_rtt_seconds_bucket{job="node"}', 0.5, "node, peer, le"), "")], unit="s", w=12, h=8,
      extra={"targets": [{"refId": "A", "datasource": DS, "expr": q('network_rtt_seconds_bucket{job="node"}', 0.5, "node, peer, le").replace("$__rate_interval", "5m"), "format": "table", "instant": True}],
             "transformations": [{"id": "groupingToMatrix", "options": {"columnField": "peer", "rowField": "node", "valueField": "Value"}}]})
n.add("table", "Tailscale path between tailscale nodes (1 = direct, 0 = DERP relay)", [("network_tailscale_direct", "")], w=12, h=8,
      extra={"targets": [{"refId": "A", "datasource": DS, "expr": "network_tailscale_direct", "format": "table", "instant": True}],
             "transformations": [{"id": "groupingToMatrix", "options": {"columnField": "peer", "rowField": "node", "valueField": "Value"}}]})
n.add("timeseries", "Node → node RTT p50 / p99", [(q('network_rtt_seconds_bucket{job="node"}', x, "node, peer, le"), "{{node}}→{{peer}} p" + str(int(x*100))) for x in (0.5, 0.99)], unit="s")
n.add("timeseries", "Packet loss", [("network_packet_loss_ratio", "{{node}}→{{peer}}"), ('network_packet_loss_ratio{job="local_probe"}', "client→{{peer}}")], unit="percentunit")
n.row("Client to node")
n.add("timeseries", "Client → node RTT p50 / p99", [(q('network_rtt_seconds_bucket{job="local_probe"}', x, "peer, le"), "{{peer}} p" + str(int(x*100))) for x in (0.5, 0.99)], unit="s")
n.add("timeseries", "TCP connect to MySQL p50", [(q('tcp_connect_duration_seconds_bucket', 0.5, "job, node, peer, le"), "{{job}} {{node}}→{{peer}}")], unit="s")
n.add("timeseries", "Network availability (probe success)", [("avg_over_time(network_probe_success[5m])", "{{job}} {{node}}→{{peer}}")], unit="percentunit")
n.add("timeseries", "TCP retransmissions / s", [("rate(node_netstat_Tcp_RetransSegs[$__rate_interval])", "{{node}}")], unit="ops")
n.add("timeseries", "Interface throughput (lab interface plus others)", [('rate(node_network_receive_bytes_total{device!~"lo|docker.*|veth.*|br-.*"}[$__rate_interval])', "{{node}} {{device}} rx"), ('rate(node_network_transmit_bytes_total{device!~"lo|docker.*|veth.*|br-.*"}[$__rate_interval])', "{{node}} {{device}} tx")], unit="Bps", w=24)
n.save(f"{out}/network.json", "lab-network", "Network", ["network"], "", [])

# ---------------- 5. Replication & HA
r = D()
NS2 = NSEL
r.row("Topology")
r.add("state-timeline", "Writable (primary) by node", [(f"ha_lab_writable{{{NS2}}}", "{{node}}")], w=12, h=6, extra={"fieldConfig": {"defaults": {"unit": "none", **STATE}, "overrides": []}})
r.add("state-timeline", "Database up by node", [(f"ha_lab_db_up{{{NS2}}}", "{{node}}")], w=12, h=6, extra={"fieldConfig": {"defaults": {"unit": "none", **UPMAP}, "overrides": []}})
r.row("Asynchronous / semi-synchronous replication")
r.add("timeseries", "Replication lag", [(f"replication_lag_seconds{{{NS2}}}", "{{node}} applier"), (f"mysql_slave_status_seconds_behind_source{{{NS2}}} or mysql_slave_status_seconds_behind_master{{{NS2}}}", "{{node}} Seconds_Behind_Source")], unit="s")
r.add("timeseries", "Replica threads running", [(f"ha_lab_replica_io_running{{{NS2}}}", "{{node}} IO"), (f"ha_lab_replica_sql_running{{{NS2}}}", "{{node}} SQL")])
r.add("timeseries", "Relay log backlog (transactions)", [(f'replication_queue_size{{queue="relay_log",{NS2}}}', "{{node}}")])
r.add("timeseries", "Semi-sync acknowledgements / s", [(f"rate(mysql_global_status_rpl_semi_sync_source_yes_tx{{{NS2}}}[$__rate_interval])", "{{node}} acked"), (f"rate(mysql_global_status_rpl_semi_sync_source_no_tx{{{NS2}}}[$__rate_interval])", "{{node}} not acked (async fallback)")], unit="ops")
r.add("timeseries", "Semi-sync status / avg wait", [(f"ha_lab_semi_sync_source_active{{{NS2}}}", "{{node}} active"), (f"mysql_global_status_rpl_semi_sync_source_tx_avg_wait_time{{{NS2}}} / 1e6", "{{node}} avg tx wait (s)")])
r.add("timeseries", "Replication position (convergence)", [(f"ha_lab_replication_position{{{NS2}}}", "{{node}}")], desc="Equal values on all nodes mean every node applied the same transactions.")
r.row("Group Replication (InnoDB Cluster)")
r.add("timeseries", "Members by state", [(f"max by (state) (ha_lab_group_members{{{NS2}}})", "{{state}}")])
r.add("timeseries", "Certification / applier queue", [(f'replication_queue_size{{queue=~"certification|applier",{NS2}}}', "{{node}} {{queue}}")])
r.add("timeseries", "Certification conflicts / s", [(f"rate(replication_certification_conflicts_total{{{NS2}}}[$__rate_interval])", "{{node}}")], unit="ops")
r.add("timeseries", "Primary member", [(f"ha_lab_group_primary{{{NS2}}}", "{{node}}")])
r.row("Galera (Percona XtraDB Cluster)")
r.add("timeseries", "Cluster size / primary component", [(f"ha_lab_wsrep_cluster_size{{{NS2}}}", "{{node}} size"), (f"ha_lab_wsrep_primary_component{{{NS2}}}", "{{node}} primary component")])
r.add("timeseries", "Receive / send queue", [(f'replication_queue_size{{queue=~"wsrep_.*",{NS2}}}', "{{node}} {{queue}}")])
r.add("timeseries", "Flow control paused (fraction)", [(f"mysql_global_status_wsrep_flow_control_paused{{{NS2}}}", "{{node}}")], unit="percentunit")
r.add("timeseries", "Certification failures / s", [(f"rate(mysql_global_status_wsrep_local_cert_failures{{{NS2}}}[$__rate_interval])", "{{node}}")], unit="ops")
r.save(f"{out}/replication-ha.json", "lab-replication-ha", "Replication & HA", ["replication"], "ha_lab_db_up")

# ---------------- 7. JMeter benchmark
j = D()
j.row("Throughput")
j.add("timeseries", "RPS / TPS", [(f"sum(rate(ha_transaction_total{{{BV}}}[$__rate_interval]))", "RPS (attempted)"), (f"sum(rate(ha_transaction_success_total{{{BV}}}[$__rate_interval]))", "TPS (committed)")], unit="ops")
j.add("timeseries", "Active threads", [(f"sum(benchmark_active_threads{{{BV}}})", "threads")])
j.add("timeseries", "Successful / failed transactions", [(f"sum by (operation) (increase(ha_transaction_success_total{{{BV}}}[$__rate_interval]))", "{{operation}} ok"), (f"sum by (operation) (increase(ha_transaction_errors_total{{{BV}}}[$__rate_interval]))", "{{operation}} failed")])
j.add("timeseries", "Error rate by class", [(f"sum by (error_class) (rate(ha_transaction_errors_total{{{BV}}}[$__rate_interval])) / scalar(sum(rate(ha_transaction_total{{{BV}}}[$__rate_interval])))", "{{error_class}}")], unit="percentunit")
j.row("Latency")
j.add("timeseries", "Latency percentiles by operation", [(q(f'ha_transaction_duration_seconds_bucket{{outcome="success",{BV}}}', x, "operation, le"), "{{operation}} p" + str(int(x*100))) for x in (0.5, 0.95, 0.99)], unit="s")
j.add("timeseries", "Average latency", [(f'sum by (operation) (rate(ha_transaction_duration_seconds_sum{{outcome="success",{BV}}}[$__rate_interval])) / sum by (operation) (rate(ha_transaction_duration_seconds_count{{outcome="success",{BV}}}[$__rate_interval]))', "{{operation}}")], unit="s")
j.save(f"{out}/jmeter-benchmark.json", "lab-jmeter", "JMeter Benchmark", ["benchmark"], "", bench_vars)

# ---------------- 8. Failover & recovery
f = D()
f.text("Failover metrics", "RTO, RPO, detection and failover durations are computed per run from the JTL, polled events and "
       "the ledger, and written to `benchmark/results/<run_id>/report.md`. Orange annotations mark benchmark runs. "
       "The panels below show the same window live.", h=3)
f.add("state-timeline", "Writable node", [("ha_lab_writable", "{{node}}")], w=24, h=6, extra={"fieldConfig": {"defaults": {"unit": "none", **STATE}, "overrides": []}})
f.add("timeseries", "Write availability (client)", [(f'sum(rate(ha_transaction_success_total{{operation="write",{BV}}}[15s])) / sum(rate(ha_transaction_total{{operation="write",{BV}}}[15s]))', "writes"), (f'sum(rate(ha_transaction_success_total{{operation="read",{BV}}}[15s])) / sum(rate(ha_transaction_total{{operation="read",{BV}}}[15s]))', "reads")], unit="percentunit")
f.add("timeseries", "Client errors by class", [(f"sum by (error_class) (rate(ha_transaction_errors_total{{{BV}}}[15s]))", "{{error_class}}")], unit="ops")
f.add("timeseries", "Successful writes / s", [(f'sum(rate(ha_transaction_success_total{{operation="write",{BV}}}[15s]))', "writes")], unit="ops")
f.add("timeseries", "Database up", [("ha_lab_db_up", "{{node}}")])
f.add("timeseries", "Replication position (consistency / catch-up)", [("ha_lab_replication_position", "{{node}}")])
f.add("timeseries", "Replication lag during recovery", [("replication_lag_seconds", "{{node}}")], unit="s")
f.save(f"{out}/failover-recovery.json", "lab-failover", "Failover & Recovery", ["failover"], "", bench_vars)
print("dashboards written to", out)
