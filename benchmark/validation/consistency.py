"""Compare acknowledged writes (JTL) with the ledger rows present on each node after the run."""

from __future__ import annotations

import json
import os


def compare(rows, ledger_dir, final_primary):
    acked, failed_definite, unknown = set(), set(), set()
    for r in rows:
        if r["op"] != "write" or not r["txn_id"]:
            continue
        if r["ok"]:
            acked.add(r["txn_id"])
        elif r["err"] == "commit_unknown":
            unknown.add(r["txn_id"])
        else:
            failed_definite.add(r["txn_id"])

    nodes = {}
    for f in sorted(os.listdir(ledger_dir)):
        if f.startswith("ledger-") and f.endswith(".json"):
            meta = json.load(open(os.path.join(ledger_dir, f)))
            name = meta["node"]
            txt = os.path.join(ledger_dir, f"ledger-{name}.txt")
            ids = None
            if meta.get("reachable") and os.path.exists(txt):
                ids = [x.strip() for x in open(txt) if x.strip()]
            parts = (meta.get("summary") or "").split()
            nodes[name] = {"reachable": ids is not None, "ids": ids,
                           "accounts_rows": parts[-2] if len(parts) >= 2 else None,
                           "accounts_balance_sum": parts[-1] if len(parts) >= 2 else None,
                           "gtid_executed": " ".join(parts[:-2]) if len(parts) > 2 else None}

    result = {"acknowledged_writes": len(acked), "failed_writes": len(failed_definite),
              "indeterminate_writes": len(unknown), "final_primary": final_primary, "nodes": {}}
    ref = None
    for name, n in nodes.items():
        if n["ids"] is None:
            result["nodes"][name] = {"reachable": False}
            continue
        present = set(n["ids"])
        dupes = len(n["ids"]) - len(present)
        entry = {
            "reachable": True,
            "rows": len(n["ids"]),
            "missing_acknowledged": len(acked - present),
            "missing_acknowledged_sample": sorted(acked - present)[:20],
            "present_but_failed": len(present & failed_definite),
            "indeterminate_committed": len(present & unknown),
            "duplicates": dupes,
            "accounts_rows": n["accounts_rows"],
            "accounts_balance_sum": n["accounts_balance_sum"],
        }
        result["nodes"][name] = entry
        if name == final_primary:
            ref = present
    if ref is not None:
        p = result["nodes"][final_primary]
        result["lost_acknowledged_writes"] = p["missing_acknowledged"]
        result["consistency_errors"] = p["missing_acknowledged"] + p["present_but_failed"] + p["duplicates"]
        diverged = [n for n, e in result["nodes"].items()
                    if e.get("reachable") and n != final_primary and set(nodes[n]["ids"]) != ref]
        sums = {e.get("accounts_balance_sum") for e in result["nodes"].values() if e.get("reachable")}
        result["diverged_nodes"] = diverged
        result["accounts_checksum_consistent"] = len(sums) <= 1
    else:
        result["lost_acknowledged_writes"] = None
        result["consistency_errors"] = None
        result["note"] = "final primary ledger unavailable"
    return result
