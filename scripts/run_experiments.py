#!/usr/bin/env python3
"""Run every controller mode against one or more scenarios, into the evidence store.

Each run gets its own directory under experiments/runs/ with a manifest; each
scenario then gets a comparison under experiments/comparisons/ that records
exactly which runs it was built from. Nothing is copied by hand.

    .venv/bin/python scripts/run_experiments.py                  # all scenarios
    .venv/bin/python scripts/run_experiments.py --scenarios gradual_coverage
    .venv/bin/python scripts/run_experiments.py --scenarios brief_disturbance \\
        --profile buffered_playback --modes measured predictive
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from echo_sim import events as E  # noqa: E402
from echo_sim import evidence as EV  # noqa: E402
from echo_sim import scenarios as S  # noqa: E402
from echo_sim.config import APPLICATION_PROFILES, RunConfig  # noqa: E402
from echo_sim.runner import record_run  # noqa: E402

COLUMNS = ("p95_ms", "late_results_pct", "results_lost", "longest_gap_ms",
           "completed_handovers", "failed_handovers", "unnecessary_reversals",
           "time_on_suboptimal_server_s", "playback_stalls")


async def main_async(args) -> None:
    for sid in args.scenarios:
        scen = S.get(sid)
        duration = args.duration or scen.default_duration_s
        run_ids = []
        print(f"\n=== {scen.title}  ({duration:.0f} s, seed {args.seed}, "
              f"{args.profile}) ===", flush=True)
        for mode in args.modes:
            cfg = RunConfig(mode=mode, scenario_id=sid, profile=args.profile,
                            seed=args.seed, duration_s=duration)
            m = await record_run(cfg)
            run_ids.append(m["run_id"])
            print(f"  {mode:14s} {m['status']:9s} {m['run_id']}", flush=True)
            await asyncio.sleep(1.0)   # let sockets settle before rebinding
        comp = EV.build_comparison(run_ids, note=args.note)
        print(f"  comparison {comp['comparison_id']}")
        print("  " + "mode".ljust(15) + "".join(c[:14].rjust(15) for c in COLUMNS))
        for mode, row in comp["modes"].items():
            print("  " + mode.ljust(15) + "".join(
                str(row["metrics"].get(c)).rjust(15) for c in COLUMNS))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenarios", nargs="+", default=list(S.SCENARIOS),
                    choices=list(S.SCENARIOS))
    ap.add_argument("--modes", nargs="+", default=list(E.CONTROLLER_MODES),
                    choices=list(E.CONTROLLER_MODES))
    ap.add_argument("--profile", default="interactive_inspection",
                    choices=list(APPLICATION_PROFILES))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--duration", type=float, default=None,
                    help="seconds; default is each scenario's own duration")
    ap.add_argument("--note", default="")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
