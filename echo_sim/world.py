"""The moving world: where the user is, and what each network looks like there.

This is the software stand-in for `tc`/`netem`. On Linux you would push these
numbers into qdiscs; here the impairment relays in netem.py read them live.
The important property either way is that conditions *drift* rather than flip,
because a smooth degradation is what gives the Predictor something to catch.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .config import NETWORK_ORDER, NETWORKS, NetworkSpec


@dataclass(frozen=True)
class LiveProfile:
    """Instantaneous characteristics of one access network."""

    network_id: str
    quality: float          # 0..1 coverage quality
    rtt_ms: float
    jitter_ms: float
    loss: float
    bandwidth_mbps: float

    @property
    def available(self) -> bool:
        return self.quality > 0.02

    def as_dict(self) -> dict:
        def num(x: float):
            return None if (math.isinf(x) or math.isnan(x)) else round(x, 5)

        return {
            "network_id": self.network_id,
            "quality": round(self.quality, 4),
            "rtt_ms": num(self.rtt_ms),
            "jitter_ms": num(self.jitter_ms),
            "loss": num(self.loss),
            "bandwidth_mbps": num(self.bandwidth_mbps),
            "available": self.available,
        }


def _interp(keyframes: List[Tuple[float, float]], x: float) -> float:
    if x <= keyframes[0][0]:
        return keyframes[0][1]
    if x >= keyframes[-1][0]:
        return keyframes[-1][1]
    for (x0, y0), (x1, y1) in zip(keyframes, keyframes[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            t = (x - x0) / (x1 - x0)
            # smoothstep, so quality bends instead of turning a corner
            t = t * t * (3.0 - 2.0 * t)
            return y0 + (y1 - y0) * t
    return keyframes[-1][1]


ZONES = [
    (0.00, "Zone A - indoor lab"),
    (0.30, "Zone A/B - corridor"),
    (0.42, "Zone B - yard"),
    (0.62, "Zone B/C - perimeter"),
    (0.74, "Zone C - far perimeter"),
    (0.86, "Zone D - dock"),
]


class World:
    """Advances the user along a fixed route and reports live link conditions.

    A small amount of correlated noise is layered on top of the coverage curves
    so the Predictor has to cope with a wobbly signal rather than a clean ramp.
    """

    def __init__(self, duration_s: float, seed: int = 7) -> None:
        self.duration_s = duration_s
        self.rng = random.Random(seed)
        self.start_ts = time.monotonic()
        self._noise: Dict[str, float] = {n: 0.0 for n in NETWORK_ORDER}
        self._noise_ts = self.start_ts
        self._frozen_position: float | None = None

    # -- time / position ---------------------------------------------------

    def elapsed(self) -> float:
        return time.monotonic() - self.start_ts

    @property
    def position(self) -> float:
        """User's progress along the route, 0.0 at the lab, 1.0 at the dock."""
        if self._frozen_position is not None:
            return self._frozen_position
        return min(1.0, max(0.0, self.elapsed() / self.duration_s))

    def freeze(self, position: float) -> None:
        """Pin the position; used by tests and by replaying a recorded run."""
        self._frozen_position = position

    def zone_label(self) -> str:
        label = ZONES[0][1]
        for start, name in ZONES:
            if self.position >= start:
                label = name
        return label

    # -- link conditions ---------------------------------------------------

    def _advance_noise(self) -> None:
        now = time.monotonic()
        dt = now - self._noise_ts
        if dt < 0.05:
            return
        self._noise_ts = now
        decay = math.exp(-dt / 1.5)
        for nid in NETWORK_ORDER:
            step = self.rng.gauss(0.0, 0.06)
            self._noise[nid] = self._noise[nid] * decay + step

    def quality(self, network_id: str) -> float:
        self._advance_noise()
        spec = NETWORKS[network_id]
        base = _interp(spec.coverage, self.position)
        if base <= 0.0:
            return 0.0
        q = base + self._noise[network_id] * base
        return min(1.0, max(0.0, q))

    def profile(self, network_id: str) -> LiveProfile:
        spec: NetworkSpec = NETWORKS[network_id]
        q = self.quality(network_id)
        if q <= 0.0:
            return LiveProfile(network_id, 0.0, float("inf"), float("inf"), 1.0, 0.0)
        b_rtt, b_jit, b_loss, b_bw = spec.best
        w_rtt, w_jit, w_loss, w_bw = spec.worst
        d = 1.0 - q
        return LiveProfile(
            network_id=network_id,
            quality=q,
            rtt_ms=b_rtt + (w_rtt - b_rtt) * d,
            jitter_ms=b_jit + (w_jit - b_jit) * d,
            # loss climbs faster than linearly as coverage falls away
            loss=b_loss + (w_loss - b_loss) * (d ** 1.8),
            bandwidth_mbps=b_bw + (w_bw - b_bw) * d,
        )

    def all_profiles(self) -> Dict[str, LiveProfile]:
        return {nid: self.profile(nid) for nid in NETWORK_ORDER}

    def snapshot(self) -> dict:
        return {
            "t": round(self.elapsed(), 3),
            "position": round(self.position, 4),
            "zone": self.zone_label(),
            "networks": {nid: p.as_dict() for nid, p in self.all_profiles().items()},
        }
