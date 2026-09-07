"""Intent Agent - what is the user actually doing right now?

The best network is not an absolute. A call wants steadiness and will trade
away throughput for it; a bulk download wants raw capacity and does not care
about a 40 ms wobble; live inference wants the lowest total time from shutter
to result. So the Decision Engine is handed a different weight vector depending
on the activity, and the traffic pattern is what reveals the activity.

This also decides whether a preload buffer is worth keeping. For live traffic
it is not - you cannot pre-fetch something that has not happened yet. For
non-live traffic a few seconds of buffer covers the whole handoff, so the user
never sees it.
"""
from __future__ import annotations

from collections import deque
from typing import Deque

from ..config import INTENT_WEIGHTS, Weights

LIVE_INTENTS = {"inference", "realtime_call"}


class IntentAgent:
    def __init__(self, default: str = "inference") -> None:
        self.intent = default
        self._rates: Deque[float] = deque(maxlen=20)
        self._sizes: Deque[int] = deque(maxlen=20)

    def observe(self, frames_per_s: float, mean_payload_bytes: int) -> None:
        self._rates.append(frames_per_s)
        self._sizes.append(mean_payload_bytes)
        if not self._rates:
            return
        rate = sum(self._rates) / len(self._rates)
        size = sum(self._sizes) / len(self._sizes)
        if rate >= 8 and size < 200_000:
            self.intent = "inference"
        elif rate >= 20 and size < 4_000:
            self.intent = "realtime_call"
        elif rate < 2 and size > 500_000:
            self.intent = "bulk_transfer"
        else:
            self.intent = "streaming"

    def set_intent(self, intent: str) -> None:
        if intent in INTENT_WEIGHTS:
            self.intent = intent

    @property
    def weights(self) -> Weights:
        return INTENT_WEIGHTS[self.intent]

    @property
    def is_live(self) -> bool:
        return self.intent in LIVE_INTENTS

    @property
    def preload_buffer_s(self) -> float:
        """Seconds of content worth pre-fetching before a handoff. 0 when live."""
        if self.is_live:
            return 0.0
        return 8.0 if self.intent == "streaming" else 3.0

    def describe(self) -> str:
        return {
            "inference": "live AI inference (needs lowest total round trip)",
            "realtime_call": "live call (needs steady timing above all)",
            "bulk_transfer": "bulk transfer (needs throughput, tolerates jitter)",
            "streaming": "streaming playback (buffer absorbs short interruptions)",
        }[self.intent]
