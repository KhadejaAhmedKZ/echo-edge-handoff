"""Measurement and scoring for one experiment run.

This is the ONE metrics implementation. The live dashboard receives its numbers
from `metrics_updated` events produced by `RunMetrics.summary()`, and the
exported summary.json / comparisons are produced by the same method, so the
screen and the evidence cannot use different definitions.

Definitions (also exported verbatim in summary["definitions"]):

  response time      time from sending a request to receiving its result
  lost result        no usable result within the request timeout, or the
                     request was dropped because too many were already waiting
  late result        a lost result, or a completed one slower than the
                     analysis deadline; reported as a share of all requests
  p95 response time  95th percentile over completed requests only
  longest result gap longest interval between two consecutive results
  unnecessary reversal
                     a server switch that returns to the server it left
                     within `reversal_window_s` of leaving it

The headline number the project is judged on:

    Inference Latency Gap = P95 latency during handoff windows
                          - P95 latency while stable

"The connection stayed alive" is not the success criterion. Continuing to get
inference results, with a small spike, while both the network and the nearest
compute location change underneath you - that is the success criterion, and
this is where it gets computed.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFINITIONS = {
    "response_time": "time from sending a request to receiving its result",
    "lost_result": ("no usable result within the request timeout, or dropped "
                    "because too many requests were already waiting"),
    "late_result": ("lost, or completed slower than the analysis deadline; "
                    "share of all requests with a recorded outcome"),
    "p95_response_time": "95th percentile over completed requests only",
    "longest_result_gap": "longest interval between two consecutive results",
    "unnecessary_reversal": ("a server switch back to the server just left, "
                             "within the reversal window"),
    "time_on_suboptimal_server": ("time spent on a server other than the best "
                                  "one by a measurement-only reference scorer, "
                                  "identical for every controller mode"),
}


@dataclass
class FrameRecord:
    frame_no: int
    sent_t: float
    recv_t: Optional[float]
    e2e_ms: Optional[float]
    inference_ms: Optional[float]
    edge_id: Optional[str]
    network_id: Optional[str]
    lost: bool = False
    duplicate: bool = False
    during_handoff: bool = False
    cold_start: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def percentile(values: List[float], p: float) -> float:
    if not values:
        return float("nan")
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return xs[int(k)]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


@dataclass
class RunMetrics:
    mode: str
    frames: List[FrameRecord] = field(default_factory=list)
    handoff_windows: List[Tuple[float, float]] = field(default_factory=list)
    # Fixed wall-clock windows where the user is physically crossing between
    # zones. Identical for every mode, so the comparison cannot be gamed by a
    # mode that simply declares fewer transitions.
    crossing_windows: List[Tuple[float, float]] = field(default_factory=list)
    handoffs: List[dict] = field(default_factory=list)
    failed_handoffs: int = 0
    reconnects: int = 0
    suboptimal_edge_s: float = 0.0
    state_bytes: int = 0
    deadline_ms: float = 150.0
    thresholds_ms: Sequence[float] = (100.0, 150.0, 200.0)
    reversal_window_s: float = 10.0
    profile: str = "interactive_inspection"
    server_switches: List[dict] = field(default_factory=list)
    continuity_checks: List[dict] = field(default_factory=list)
    unfinished_at_end: int = 0
    path_migrations: int = 0
    # buffered playback
    stalls: int = 0
    stall_time_s: float = 0.0
    min_buffer_s: Optional[float] = None
    playback_started: bool = False

    # -- collection --------------------------------------------------------

    def add(self, rec: FrameRecord) -> None:
        rec.during_handoff = any(a <= rec.sent_t <= b for a, b in self.handoff_windows)
        self.frames.append(rec)

    def note_server_switch(self, t: float, from_server: Optional[str],
                           to_server: str, kind: str) -> None:
        if from_server == to_server:
            return
        self.server_switches.append({"t": round(t, 3), "from": from_server,
                                     "to": to_server, "kind": kind})

    def unnecessary_reversals(self) -> int:
        n = 0
        sw = self.server_switches
        for i in range(1, len(sw)):
            prev, cur = sw[i - 1], sw[i]
            if (cur["to"] == prev["from"]
                    and cur["t"] - prev["t"] <= self.reversal_window_s):
                n += 1
        return n

    def late(self, threshold_ms: float) -> Tuple[int, float]:
        total = len(self.frames)
        late = sum(1 for f in self.frames
                   if f.lost or (f.e2e_ms is not None and f.e2e_ms > threshold_ms))
        return late, (round(100.0 * late / total, 2) if total else 0.0)

    def mark_handoff_window(self, start_t: float, end_t: float) -> None:
        self.handoff_windows.append((start_t, end_t))
        for rec in self.frames:
            if start_t <= rec.sent_t <= end_t:
                rec.during_handoff = True

    # -- views -------------------------------------------------------------

    @property
    def delivered(self) -> List[FrameRecord]:
        return [f for f in self.frames if f.e2e_ms is not None and not f.lost]

    def in_crossing(self, t: float) -> bool:
        return any(a <= t <= b for a, b in self.crossing_windows)

    def crossing_latencies(self, inside: bool) -> List[float]:
        return [f.e2e_ms for f in self.delivered
                if self.in_crossing(f.sent_t) == inside]

    def latencies(self, during_handoff: Optional[bool] = None) -> List[float]:
        out = []
        for f in self.delivered:
            if during_handoff is None or f.during_handoff == during_handoff:
                out.append(f.e2e_ms)
        return out

    # -- summary -----------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        all_lat = self.latencies()
        stable = self.latencies(during_handoff=False)
        during = self.latencies(during_handoff=True)
        lost = [f for f in self.frames if f.lost]

        p95_stable = percentile(stable, 0.95)
        p95_handoff = percentile(during, 0.95) if during else p95_stable
        gap = (p95_handoff - p95_stable) if all_lat else float("nan")

        return {
            "mode": self.mode,
            "frames_sent": len(self.frames),
            "frames_delivered": len(all_lat),
            "frames_lost": len(lost),
            "frames_duplicated": sum(1 for f in self.frames if f.duplicate),
            "loss_pct": round(100.0 * len(lost) / max(1, len(self.frames)), 2),
            "mean_ms": round(statistics.fmean(all_lat), 2) if all_lat else None,
            "median_ms": round(percentile(all_lat, 0.5), 2) if all_lat else None,
            "p95_ms": round(percentile(all_lat, 0.95), 2) if all_lat else None,
            "p99_ms": round(percentile(all_lat, 0.99), 2) if all_lat else None,
            "max_ms": round(max(all_lat), 2) if all_lat else None,
            "p95_stable_ms": round(p95_stable, 2) if stable else None,
            "p95_handoff_ms": round(p95_handoff, 2) if during else None,
            "inference_latency_gap_ms": round(gap, 2) if all_lat else None,
            "p95_crossing_ms": round(percentile(self.crossing_latencies(True), 0.95), 2)
            if self.crossing_latencies(True) else None,
            "p95_settled_ms": round(percentile(self.crossing_latencies(False), 0.95), 2)
            if self.crossing_latencies(False) else None,
            "zone_gap_ms": round(
                percentile(self.crossing_latencies(True), 0.95)
                - percentile(self.crossing_latencies(False), 0.95), 2)
            if self.crossing_latencies(True) and self.crossing_latencies(False) else None,
            "crossing_loss_pct": round(
                100.0 * sum(1 for f in self.frames
                            if f.lost and self.in_crossing(f.sent_t))
                / max(1, sum(1 for f in self.frames if self.in_crossing(f.sent_t))), 2),
            "handoffs": len(self.handoffs),
            "failed_handoffs": self.failed_handoffs,
            "reconnects": self.reconnects,
            "mean_handoff_ms": round(
                statistics.fmean([h["total_ms"] for h in self.handoffs]), 2)
            if self.handoffs else None,
            "state_bytes_transferred": self.state_bytes,
            "time_on_suboptimal_edge_s": round(self.suboptimal_edge_s, 2),
            "longest_gap_ms": round(self.longest_gap_ms(), 2),
            **self._experience(all_lat, lost),
        }

    def _experience(self, all_lat: List[float], lost: List[FrameRecord]) -> Dict[str, Any]:
        """Application-experience metrics, named the way the dashboard shows them."""
        completed = [f for f in self.frames if f.e2e_ms is not None and not f.lost]
        late_by = {}
        for t in sorted(set(list(self.thresholds_ms) + [self.deadline_ms])):
            n, pct = self.late(t)
            late_by[str(int(t))] = {"late": n, "pct": pct}
        dl_n, dl_pct = self.late(self.deadline_ms)
        checks_ok = sum(1 for c in self.continuity_checks if c.get("ok"))
        return {
            "profile": self.profile,
            "requests_generated": len(self.frames),
            "results_received": len(completed),
            "results_lost": len(lost),
            "unfinished_at_end": self.unfinished_at_end,
            "current_response_ms": (round(completed[-1].e2e_ms, 1)
                                    if completed else None),
            "deadline_ms": self.deadline_ms,
            "late_results": dl_n,
            "late_results_pct": dl_pct,
            "late_by_threshold": late_by,
            "completed_handovers": len(self.handoffs),
            "failed_handovers": self.failed_handoffs,
            "server_switches": len(self.server_switches),
            "path_migrations": self.path_migrations,
            "unnecessary_reversals": self.unnecessary_reversals(),
            "reversal_window_s": self.reversal_window_s,
            "time_on_suboptimal_server_s": round(self.suboptimal_edge_s, 2),
            "continuity_checks_passed": checks_ok,
            "continuity_checks_failed": len(self.continuity_checks) - checks_ok,
            "playback_stalls": self.stalls if self.profile == "buffered_playback" else None,
            "playback_stall_time_s": (round(self.stall_time_s, 2)
                                      if self.profile == "buffered_playback" else None),
            "min_buffer_s": (round(self.min_buffer_s, 2)
                             if self.min_buffer_s is not None else None),
        }

    def longest_gap_ms(self) -> float:
        """Longest stretch with no inference result at all - the visible freeze."""
        got = sorted(f.recv_t for f in self.delivered)
        if len(got) < 2:
            return 0.0
        return max((b - a) for a, b in zip(got, got[1:])) * 1000.0

    def to_json(self, path: str) -> None:
        payload = {
            "summary": self.summary(),
            "definitions": DEFINITIONS,
            "server_switches": self.server_switches,
            "continuity_checks": self.continuity_checks,
            "handoffs": self.handoffs,
            "handoff_windows": self.handoff_windows,
            "crossing_windows": self.crossing_windows,
            "frames": [f.as_dict() for f in self.frames],
        }
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
