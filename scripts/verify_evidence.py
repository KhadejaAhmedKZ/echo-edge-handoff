#!/usr/bin/env python3
"""Check that every run's files still match its manifest, and say which runs
were produced by the engine code currently on disk.

    .venv/bin/python scripts/verify_evidence.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from echo_sim import evidence as EV  # noqa: E402


def main() -> int:
    current = EV.source_hash()
    print(f"engine source sha256 on disk: {current[:12]}")
    bad = 0
    for r in EV.list_runs():
        rid = r["run_id"]
        if r.get("status") == "in_progress_or_incomplete":
            print(f"  INCOMPLETE  {rid}")
            continue
        v = EV.verify_run(rid)
        m = EV.read_json(rid, "manifest.json")
        same = m["code_version"].get("source_sha256") == current
        tag = "ok " if v["ok"] else "BAD"
        bad += 0 if v["ok"] else 1
        print(f"  {tag} {rid}  origin={m['data_origin']}  "
              f"code={'current' if same else m['code_version'].get('identifier')}"
              + ("" if v["ok"] else f"  {v['problems']}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
