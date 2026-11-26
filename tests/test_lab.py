"""Offline checks of the runner and report logic: python3 -m unittest discover tests"""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for d in ("benchmark/runner", "benchmark/report", "benchmark/correlation", "metrics/collectors"):
    sys.path.insert(0, os.path.join(ROOT, d))

import analyze  # noqa: E402
import chaos  # noqa: E402
import compare  # noqa: E402
import loadhost  # noqa: E402
import run  # noqa: E402
import timeline  # noqa: E402


def server(reply):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)

    def loop():
        while True:
            c, _ = s.accept()
            if reply is not None:
                c.settimeout(1)
                try:
                    c.recv(8) if reply == b"S" else None
                except OSError:
                    pass
                c.sendall(reply)
                c.close()
    if reply is not None:
        threading.Thread(target=loop, daemon=True).start()
    return s, s.getsockname()[1]


class Probe(unittest.TestCase):
    def test_mysql_greeting(self):
        _, port = server(b"J\x00\x00\x00\x0a8.4")
        self.assertTrue(run.db_alive("127.0.0.1", port, "mysql", 1))

    def test_postgres_ssl_reply(self):
        _, port = server(b"S")
        self.assertTrue(run.db_alive("127.0.0.1", port, "postgres", 1))

    def test_frozen_server_is_down(self):
        _, port = server(None)
        self.assertFalse(run.db_alive("127.0.0.1", port, "postgres", 0.3))


class Plan(unittest.TestCase):
    def test_parse(self):
        p = chaos.parse_plan("360:freeze/30@db-02 180:kill-db")
        self.assertEqual([i["at"] for i in p], [180, 360])
        self.assertEqual(p[1], {"at": 360, "fault": "freeze", "duration": 30, "node": "db-02"})

    def test_bad_item(self):
        with self.assertRaises(SystemExit):
            chaos.parse_plan("10:bogus")


class Analysis(unittest.TestCase):
    def rows(self):
        base = 1_000_000
        out = []
        for i in range(100):
            op = "report" if i % 10 == 0 else ("write" if i % 2 else "read")
            out.append({"start_ms": base + i * 100, "end_ms": base + i * 100 + 50, "op": op, "ok": i != 51,
                        "txn_id": f"t{i}" if op == "write" else "", "err": "" if i != 51 else "timeout", "txn_s": 0.05,
                        "connect_s": 0, "exec_s": 0.04, "commit_s": 0.01, "served_by": "n1", "threads": 4,
                        "visible_ms": (i % 7) if op == "write" else None})
        return out

    def test_report_traffic_not_in_tps(self):
        s = analyze.summarize(self.rows())
        self.assertEqual(s["samples"], 90)
        self.assertGreater(s["report_qps"], 0)

    def test_visibility(self):
        v = analyze.visibility(self.rows())
        self.assertEqual(v["checked"], 50)
        self.assertEqual(v["not_visible"], 0)

    def test_timeline_svg(self):
        svg = timeline.build(self.rows(), [{"event": "fault_injected", "ts": 1001.0}], [], 1000.0)
        self.assertIn("<svg", svg)
        self.assertIn("fault injected", svg)


class Compare(unittest.TestCase):
    def test_bars(self):
        svg = compare.bars("TPS", [("a", [{"tps": 10}, {"tps": 12}]), ("b", [{"tps": None}])], "tps", 1)
        self.assertIn("a", svg)
        self.assertNotIn(">b<", svg)


class LoadHost(unittest.TestCase):
    def test_remap(self):
        root, rd = "/r", "/r/benchmark/results/run-1"
        cmd = ["/r/local/tools/jmeter/bin/jmeter", "-q", "/r/jmeter/lab.properties", "-q", rd + "/secret.properties",
               "-Jlab_script_dir=/r/jmeter/scripts", "-l", rd + "/jmeter.jtl"]
        out = loadhost.remap(cmd, root, rd, "/h/halab-jmeter")
        self.assertEqual(out[0], "/h/halab-jmeter/jmeter/bin/jmeter")
        self.assertEqual(out[4], "/h/halab-jmeter/run/secret.properties")
        self.assertEqual(out[5], "-Jlab_script_dir=/h/halab-jmeter/lab/scripts")
        self.assertEqual(out[7], "/h/halab-jmeter/run/jmeter.jtl")


class Entrypoints(unittest.TestCase):
    """Scripts run as make runs them (python3 -I): imports must resolve without the test's sys.path."""
    def test_help(self):
        import subprocess
        for script in ("benchmark/runner/run.py", "benchmark/runner/chaos.py", "benchmark/runner/suite.py",
                       "benchmark/report/compare.py", "benchmark/report/report.py", "benchmark/runner/screenshots.py"):
            p = subprocess.run([sys.executable, "-I", os.path.join(ROOT, script), "--help"], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, f"{script}: {p.stderr[-300:]}")


class Settings(unittest.TestCase):
    def test_ha_settings(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "cfg"))
            json.dump({"semi_sync": {"timeout_ms": 1000}, "mysql": {"config": {"x": 1}}},
                      open(os.path.join(d, "cfg", "ha.yml"), "w"))
            old = run.ROOT
            try:
                run.ROOT, os.environ["LAB_CONFIG_DIR"] = d, "cfg"
                self.assertEqual(run.ha_settings({"ha_provider": "semi-sync", "db_engine": "mysql"}),
                                 {"timeout_ms": 1000, "server_config": {"x": 1}})
            finally:
                run.ROOT = old
                del os.environ["LAB_CONFIG_DIR"]


if __name__ == "__main__":
    unittest.main()
