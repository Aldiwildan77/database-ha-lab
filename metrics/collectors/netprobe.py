"""Shared network probing (stdlib only): ICMP ping, tailscale ping, TCP connect timing."""

from __future__ import annotations

import re
import socket
import subprocess
import time

RTT_RE = re.compile(r"time[=<]([\d.]+) ?ms")
TS_RE = re.compile(r"pong from .* via (\S+.*?) in ([\d.]+)ms")


def ping(host: str, count: int = 5, interval: float = 0.2, timeout: int = 1):
    """Returns (rtts_seconds, sent, received)."""
    cmd = ["ping", "-n", "-c", str(count), "-i", str(interval)]
    cmd += ["-W", str(timeout * 1000 if _is_macos() else timeout)]
    try:
        out = subprocess.run(cmd + [host], capture_output=True, text=True, timeout=count * (interval + timeout) + 5).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return [], count, 0
    rtts = [float(x) / 1000 for x in RTT_RE.findall(out)]
    return rtts, count, len(rtts)


def tailscale_ping(host: str, timeout: str = "3s"):
    """Returns (direct: bool|None, rtt_seconds|None, path: str)."""
    try:
        out = subprocess.run(["tailscale", "ping", "--c", "1", "--timeout", timeout, host],
                             capture_output=True, text=True, timeout=10).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None, None, ""
    m = TS_RE.search(out)
    if not m:
        return None, None, out.strip()[-120:]
    path = m.group(1)
    return not path.startswith("DERP"), float(m.group(2)) / 1000, path


def tcp_connect(host: str, port: int, timeout: float = 3.0):
    """Returns connect duration in seconds, or None on failure. Monotonic clock."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return time.perf_counter() - t0
    except OSError:
        return None


def _is_macos():
    import platform
    return platform.system() == "Darwin"
