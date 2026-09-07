"""Troubleshooter - name the real cause when things feel bad.

Not every bad moment is fixed by switching, and "your connection is slow" is
not an answer. This agent separates the cases that actually have different
causes: congestion on the path, an overloaded edge, coverage falling away, an
unstable link that is fast on average but shaky, or the honest case where the
network is fine and the compute is simply the bottleneck.
"""
from __future__ import annotations

from typing import Optional

from .decision import Candidate
from .predictor import Predictor
from .watcher import Watcher

CAUSES = {
    "coverage": "coverage is falling away",
    "congestion": "the path is congested",
    "instability": "the link is unstable rather than slow",
    "loss": "packets are being lost and resent",
    "edge_load": "the edge server is overloaded",
    "compute_bound": "the network is fine; inference itself is the bottleneck",
    "satellite_physics": "you are on satellite, where the distance itself is the delay",
    "healthy": "nothing is wrong",
}


class Troubleshooter:
    def __init__(self, watcher: Watcher, predictor: Predictor) -> None:
        self.watcher = watcher
        self.predictor = predictor

    def diagnose(self, current: Candidate, e2e_ms: Optional[float] = None) -> dict:
        prof = self.watcher.latest_networks.get(current.network_id)
        health = self.watcher.latest_edges.get(current.edge_id, {})
        pred = self.predictor.network(current.network_id)
        observed = e2e_ms if e2e_ms is not None else current.e2e_ms

        cause = "healthy"
        detail = ""

        if prof is None or not prof.available:
            cause, detail = "coverage", "the network you were on has no coverage here"
        elif current.network_id == "satellite" and current.path_rtt_ms > 400:
            cause = "satellite_physics"
            detail = (f"satellite round trip is {current.path_rtt_ms:.0f} ms before "
                      f"anything is computed")
        elif pred["quality"] < 0.35 and pred["quality_slope"] < -0.01:
            cause = "coverage"
            detail = (f"coverage is at {pred['quality'] * 100:.0f}% and dropping "
                      f"{abs(pred['quality_slope']) * 100:.0f} points per second")
        elif current.loss > 0.02:
            cause = "loss"
            detail = f"{current.loss * 100:.1f}% of packets are being lost and resent"
        elif current.jitter_ms > 12:
            cause = "instability"
            detail = (f"jitter is {current.jitter_ms:.0f} ms, so timing is uneven "
                      f"even though average latency looks acceptable")
        elif health.get("cpu", 0) > 0.8:
            cause = "edge_load"
            detail = (f"{current.edge_id} is at {health['cpu'] * 100:.0f}% CPU with "
                      f"{health.get('active_sessions', 0)} sessions")
        elif current.inference_ms > current.path_rtt_ms * 1.5:
            cause = "compute_bound"
            detail = (f"{current.inference_ms:.0f} ms of the {observed:.0f} ms is "
                      f"inference, not transit")

        return {
            "cause": cause,
            "headline": CAUSES[cause],
            "detail": detail,
            "e2e_ms": round(observed, 1),
            "network_id": current.network_id,
            "edge_id": current.edge_id,
        }
