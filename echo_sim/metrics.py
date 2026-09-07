"""Measurement and scoring for one experiment run.

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
from typing import Any, Dict, List, Optional, Tuple


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

    # -- collection --------------------------------------------------------

    def add(self, rec: FrameRecord) -> None:
        rec.during_handoff = any(a <= rec.sent_t <= b for a, b in self.handoff_windows)
        self.frames.append(rec)

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
            "handoffs": self.handoffs,
            "handoff_windows": self.handoff_windows,
            "crossing_windows": self.crossing_windows,
            "frames": [f.as_dict() for f in self.frames],
        }
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
