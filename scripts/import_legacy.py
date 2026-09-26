#!/usr/bin/env python3
"""Import the pre-manifest results (results/run-{tcp,quic,echo}.json) as
labelled, imported runs in the evidence store.

These are the runs behind the README's original results table (90 s, seed 7).
They were produced before the manifest existed, so their code version is not
known and their scenario is the original route (recorded here as
gradual_coverage v0). The importer:

  * keeps the original summary untouched as `original_summary`,
  * recomputes the summary from the recorded per-request data with the shared
    metrics implementation, so the dashboard shows the same definitions for
    every run,
  * synthesises a replayable event stream from the per-request records and
    the handover records (flagged source_component="importer"),
  * marks the run data_origin="imported".

Nothing is invented: requests, results and handovers come from the file; the
map position during replay is derived from time, as the original route did.

    .venv/bin/python scripts/import_legacy.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from echo_sim import evidence as EV  # noqa: E402
from echo_sim.config import DEADLINE_THRESHOLDS_MS  # noqa: E402
from echo_sim.events import LEGACY_MODE_ALIASES, SCHEMA_VERSION  # noqa: E402
from echo_sim.metrics import FrameRecord, RunMetrics  # noqa: E402
from echo_sim.runner import describe_world  # noqa: E402

RESULTS = os.path.join(EV.ROOT, "results")


def _sha(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def import_one(legacy_mode: str) -> str | None:
    src = os.path.join(RESULTS, f"run-{legacy_mode}.json")
    if not os.path.exists(src):
        print(f"  skip {legacy_mode}: {src} not found")
        return None
    digest = _sha(src)
    mode = LEGACY_MODE_ALIASES[legacy_mode]
    run_id = f"imported-legacy-gradual_coverage-{mode}-s7-{digest[:8]}"
    if os.path.isdir(os.path.join(EV.RUNS, run_id)):
        print(f"  already imported: {run_id}")
        return run_id
    with open(src) as fh:
        data = json.load(fh)

    os.makedirs(os.path.join(EV.RUNS, run_id))
    d = os.path.join(EV.RUNS, run_id)
    duration = 90.0
    configuration = {
        "run_id": run_id,
        "controller_mode": mode,
        "legacy_mode_name": legacy_mode,
        "scenario_id": "gradual_coverage",
        "scenario_version": 0,
        "scenario": {"id": "gradual_coverage", "version": 0,
                     "title": "A - Gradual coverage change (original route, pre-manifest)",
                     "route": [0.0, 1.0], "disturbances": [], "faults": [],
                     "summary": "Original four-zone route used for the README table."},
        "application_profile": "interactive_inspection",
        "application_profile_label": "Interactive inspection",
        "seed": 7, "duration_s": duration, "request_rate_hz": 20.0,
        "deadline_ms": 150.0,
        "prediction_horizon_s": 2.0 if mode == "predictive" else None,
        "world": describe_world(),
        "crossing_windows_s": data.get("crossing_windows", []),
        "status": {"origin": "imported from results/run-%s.json" % legacy_mode,
                   "code_version": "not recorded (pre-manifest)"},
        "import_source": {"file": f"results/run-{legacy_mode}.json", "sha256": digest},
    }
    with open(os.path.join(d, "configuration.json"), "w") as fh:
        json.dump(configuration, fh, indent=2)

    # Recompute with the shared implementation.
    m = RunMetrics(mode=mode, deadline_ms=150.0, thresholds_ms=DEADLINE_THRESHOLDS_MS)
    m.crossing_windows = [tuple(w) for w in data.get("crossing_windows", [])]
    m.handoff_windows = [tuple(w) for w in data.get("handoff_windows", [])]
    for f in data["frames"]:
        m.frames.append(FrameRecord(**f))
    m.handoffs = list(data.get("handoffs", []))
    orig = data["summary"]
    m.failed_handoffs = orig.get("failed_handoffs", 0)
    m.reconnects = orig.get("reconnects", 0)
    m.suboptimal_edge_s = orig.get("time_on_suboptimal_edge_s", 0.0)
    m.state_bytes = orig.get("state_bytes_transferred", 0)
    windows = data.get("handoff_windows", [])
    if mode == "predictive":
        for h, w in zip(m.handoffs, windows):
            m.note_server_switch(w[1], h["source_edge"], h["target_edge"], "handoff")
    summary = m.summary()

    # Replayable stream from the recorded requests and handovers.
    seq = [0]
    rows = []

    def ev(t, et, **payload):
        seq[0] += 1
        rows.append({"run_id": run_id, "sequence_number": seq[0],
                     "elapsed_time": round(t, 4), "event_type": et,
                     "controller_mode": mode, "scenario_id": "gradual_coverage",
                     "source_component": "importer", "payload": payload})

    ev(0.0, "mission_started", scenario_id="gradual_coverage", scenario_version=0,
       controller_mode=mode, seed=7, duration_s=duration,
       application_profile="interactive_inspection", imported=True)
    timeline = []
    for f in data["frames"]:
        t = f["recv_t"] if f.get("recv_t") is not None else f["sent_t"]
        timeline.append((t, "f", f))
    if mode == "predictive":
        for h, w in zip(m.handoffs, windows):
            timeline.append((w[0], "hs", h))
            timeline.append((w[1], "hc", h))
    timeline.sort(key=lambda x: x[0])
    last_obs = -1.0
    current = None
    for t, kind, obj in timeline:
        if kind == "f":
            f = obj
            if f.get("edge_id"):
                current = (f["edge_id"], f.get("network_id"))
            if t - last_obs >= 0.25 and current:
                pos = min(1.0, max(0.0, t / duration))
                ev(t, "network_observed", position=round(pos, 4), zone="",
                   networks={}, servers={}, current_server=current[0],
                   current_network=current[1], imported=True)
                last_obs = t
            if f["lost"]:
                ev(t, "request_lost", request_no=f["frame_no"], sent_t=f["sent_t"],
                   reason="recorded as lost", server=f.get("edge_id"),
                   network=f.get("network_id"))
            else:
                ev(t, "request_completed", request_no=f["frame_no"],
                   sent_t=f["sent_t"], recv_t=f["recv_t"],
                   response_ms=round(f["e2e_ms"], 2),
                   processing_ms=f.get("inference_ms"), server=f.get("edge_id"),
                   network=f.get("network_id"), duplicate=f.get("duplicate", False))
        elif kind == "hs":
            ev(t, "handoff_phase_changed", phase="selected",
               source_server=obj["source_edge"], target_server=obj["target_edge"],
               target_network=obj["target_network"])
        else:
            h = dict(obj)
            ev(t, "handoff_completed", source_server=h.pop("source_edge"),
               target_server=h.pop("target_edge"), **h)
    ev(duration, "mission_completed", summary=summary, imported=True)
    with open(os.path.join(d, "events.jsonl"), "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    EV.finalize_run_dir(
        d, run_id, summary, "completed", EV.IMPORTED, "unknown (pre-manifest)",
        {"original_summary": orig,
         "import_note": ("Recomputed from recorded per-request data with the "
                         "shared metrics implementation. Server-switch and "
                         "continuity metrics were not recorded by the original "
                         "run and are absent or zero."),
         "code_version": {"identifier": "legacy (not recorded)", "git_commit": None,
                          "source_sha256": None,
                          "note": "produced before manifests existed"},
         "dependencies": {"note": "not recorded"}})
    print(f"  imported {run_id}")
    return run_id


def main() -> None:
    ids = [r for r in (import_one(m) for m in ("tcp", "quic", "echo")) if r]
    if len(ids) == 3:
        existing = [c for c in EV.list_comparisons()
                    if c.get("note", "").startswith("Imported legacy")]
        if not existing:
            comp = EV.build_comparison(
                ids, note="Imported legacy results (README table, pre-manifest). "
                          "No measurement-driven run exists for this set.")
            print(f"  comparison {comp['comparison_id']}")


if __name__ == "__main__":
    main()
