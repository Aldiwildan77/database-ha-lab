"""Minimal Prometheus exposition helpers (stdlib only)."""

from __future__ import annotations

import http.server
import math
import threading


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(d: dict) -> str:
    if not d:
        return ""
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in sorted(d.items())) + "}"


def _num(v: float) -> str:
    if v == math.inf:
        return "+Inf"
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return repr(float(v))


DEFAULT_BUCKETS = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)


class Registry:
    def __init__(self):
        self._families: dict[str, tuple[str, str, list]] = {}
        self._lock = threading.Lock()

    def _family(self, name, mtype, help_):
        if name not in self._families:
            self._families[name] = (mtype, help_, [])
        return self._families[name][2]

    def gauge(self, name, value, labels=None, help_=""):
        if value is None:
            return
        with self._lock:
            self._family(name, "gauge", help_).append((name, labels or {}, float(value)))

    def counter(self, name, value, labels=None, help_=""):
        if value is None:
            return
        with self._lock:
            self._family(name, "counter", help_).append((name, labels or {}, float(value)))

    def histogram(self, name, cumulative: list[tuple[float, float]], total_sum, labels=None, help_=""):
        """cumulative: [(le, cumulative_count)] ending with (inf, count)."""
        labels = labels or {}
        with self._lock:
            fam = self._family(name, "histogram", help_)
            for le, c in cumulative:
                fam.append((name + "_bucket", {**labels, "le": _num(le)}, float(c)))
            fam.append((name + "_sum", labels, float(total_sum)))
            fam.append((name + "_count", labels, float(cumulative[-1][1] if cumulative else 0)))

    def render(self) -> str:
        out = []
        with self._lock:
            for name, (mtype, help_, samples) in sorted(self._families.items()):
                if help_:
                    out.append(f"# HELP {name} {help_}")
                out.append(f"# TYPE {name} {mtype}")
                for sname, labels, value in samples:
                    out.append(f"{sname}{_labels(labels)} {_num(value)}")
        return "\n".join(out) + "\n"


class Histogram:
    """Cumulative in-process histogram keyed by label tuples."""

    def __init__(self, name, help_, buckets=DEFAULT_BUCKETS):
        self.name, self.help, self.buckets = name, help_, tuple(sorted(buckets))
        self._data: dict[tuple, list] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, **labels):
        key = tuple(sorted(labels.items()))
        with self._lock:
            d = self._data.setdefault(key, [[0] * len(self.buckets), 0.0, 0])
            for i, b in enumerate(self.buckets):
                if value <= b:
                    d[0][i] += 1
            d[1] += value
            d[2] += 1

    def export(self, reg: Registry):
        with self._lock:
            for key, (counts, s, n) in self._data.items():
                cum = [(b, counts[i]) for i, b in enumerate(self.buckets)] + [(math.inf, n)]
                reg.histogram(self.name, cum, s, dict(key), self.help)

    def state(self):
        with self._lock:
            return {"|".join(f"{k}={v}" for k, v in key): v for key, v in self._data.items()}

    def load(self, state: dict):
        for k, v in (state or {}).items():
            key = tuple(tuple(p.split("=", 1)) for p in k.split("|") if p)
            if len(v[0]) == len(self.buckets):
                self._data[key] = v


class Counter:
    def __init__(self, name, help_):
        self.name, self.help = name, help_
        self._data: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def inc(self, amount=1.0, **labels):
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._data[key] = self._data.get(key, 0.0) + amount

    def export(self, reg: Registry):
        with self._lock:
            for key, v in self._data.items():
                reg.counter(self.name, v, dict(key), self.help)


def serve(port: int, collect, bind: str = ""):
    """Serve /metrics; collect(registry) fills a fresh Registry per scrape."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] not in ("/metrics", "/"):
                self.send_error(404)
                return
            reg = Registry()
            try:
                collect(reg)
                body, code = reg.render().encode(), 200
            except Exception as e:  # surface errors as a scrape failure
                body, code = f"collect failed: {e}\n".encode(), 500
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True
    srv.serve_forever()
