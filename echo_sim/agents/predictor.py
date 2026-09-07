"""Predictor - looks at where a link is heading, not just where it is.

A current-value system reacts after the fact: by the time Wi-Fi latency is
visibly bad, the frame that mattered has already been late. This agent fits a
short-window trend to each metric and extrapolates it over a horizon, so the
Decision Engine is scoring the network the user is about to have.

The fit is a least-squares slope over the recent samples with an exponentially
weighted level. That is deliberately simple: the argument this project makes is
about acting early, not about the sophistication of the estimator, and a linear
trend is enough to catch a coverage ramp. Swapping in an ARIMA or a small
learned model is a drop-in change to `_trend`.
"""
from __future__ import annotations

from typing import Deque, Tuple

from .watcher import Sample, Watcher


def _trend(samples: Deque[Sample], alpha: float = 0.4) -> Tuple[float, float]:
    """Return (ewma_level, slope_per_second) for a metric window."""
    if not samples:
        return 0.0, 0.0
    level = samples[0].value
    for s in list(samples)[1:]:
        level = alpha * s.value + (1 - alpha) * level
    if len(samples) < 4:
        return level, 0.0

    recent = list(samples)[-16:]
    t0 = recent[0].t
    xs = [s.t - t0 for s in recent]
    ys = [s.value for s in recent]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 1e-9:
        return level, 0.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    return level, slope


class Predictor:
    def __init__(self, watcher: Watcher, horizon_s: float = 2.0) -> None:
        self.watcher = watcher
        self.horizon_s = horizon_s

    def network(self, network_id: str) -> dict:
        """Predicted characteristics of one access network `horizon` from now."""
        h = self.watcher.net_history[network_id]
        out = {}
        for metric in ("rtt_ms", "jitter_ms", "loss", "bandwidth_mbps", "quality"):
            level, slope = _trend(h[metric])
            predicted = level + slope * self.horizon_s
            out[metric] = predicted
            out[metric + "_slope"] = slope
        out["rtt_ms"] = max(0.5, out["rtt_ms"])
        out["jitter_ms"] = max(0.0, out["jitter_ms"])
        out["loss"] = min(1.0, max(0.0, out["loss"]))
        out["bandwidth_mbps"] = max(0.1, out["bandwidth_mbps"])
        out["quality"] = min(1.0, max(0.0, out["quality"]))
        return out

    def edge_inference_ms(self, edge_id: str) -> float:
        level, slope = _trend(self.watcher.edge_history[edge_id])
        return max(1.0, level + slope * self.horizon_s)

    def degrading(self, network_id: str, rtt_slope_threshold: float = 6.0,
                  quality_slope_threshold: float = -0.05) -> bool:
        """True when this network is trending worse fast enough to act on."""
        p = self.network(network_id)
        return (p["rtt_ms_slope"] > rtt_slope_threshold
                or p["quality_slope"] < quality_slope_threshold
                or p["loss_slope"] > 0.01)

    def time_to_unusable_s(self, network_id: str) -> float:
        """Rough seconds until coverage hits zero at the current slope."""
        p = self.network(network_id)
        q, slope = p["quality"], p["quality_slope"]
        if slope >= -1e-4:
            return float("inf")
        return max(0.0, q / -slope)
