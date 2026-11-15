#!/usr/bin/env python3
"""ha-lab node collector: loads the engine module named in the config and serves its metrics.

Config (/etc/ha-lab/collector.json) carries "engine" (default "mysql"). The engine module is
engine_<engine>.py in this directory and exposes Collector(cfg) with .lock and .collect(registry).
Every engine emits the same engine-neutral topology metrics (ha_lab_db_up, ha_lab_writable,
ha_lab_read_only, ha_lab_replication_position) so failover detection works for any database.
"""

from __future__ import annotations

import importlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import promlib  # noqa: E402

CONFIG = os.environ.get("HA_LAB_COLLECTOR_CONFIG", "/etc/ha-lab/collector.json")


def main():
    with open(CONFIG) as fh:
        cfg = json.load(fh)
    engine = str(cfg.get("engine", "mysql"))
    if not engine.isidentifier():
        raise SystemExit(f"invalid engine name: {engine!r}")
    try:
        module = importlib.import_module(f"engine_{engine}")
    except ModuleNotFoundError as exc:
        raise SystemExit(f"no collector for engine '{engine}': {exc}")
    col = module.Collector(cfg)

    def collect(reg):
        with col.lock:
            col.collect(reg)

    promlib.serve(int(cfg.get("port", 9106)), collect)


if __name__ == "__main__":
    main()
