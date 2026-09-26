"""Intent Agent - which weighting should the Decision Engine use?

For the submitted prototype the application profile is chosen explicitly
(`Interactive inspection` or `Buffered playback`). Automatic identification of
an application from encrypted traffic is NOT claimed: the traffic-shape
classifier below exists, is tested, and reports "unknown" when it cannot tell,
but it only advises. It never overrides an explicitly selected profile.

Earlier versions had an unreachable branch: the broad "inference" rule
(rate >= 8, size < 200 kB) matched every call-like pattern first, so
"realtime_call" could never be returned. The rules below are mutually
exclusive and checked most-specific first.
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Optional

from ..config import APPLICATION_PROFILES, INTENT_WEIGHTS, Weights

LIVE_INTENTS = {"inference", "realtime_call"}
UNKNOWN = "unknown"
# When the traffic cannot be classified, weight it as live traffic: treating
# live traffic as bufferable is the costly mistake, the reverse is not.
CONSERVATIVE_INTENT = "inference"


def classify_traffic(rate_per_s: float, mean_payload_bytes: float) -> str:
    """Advisory classification of a traffic shape. Returns an intent or 'unknown'.

    Rules are disjoint on purpose, most specific first:
      realtime_call  - many small packets        (>= 20 /s, < 4 kB)
      inference      - steady medium frames      (>= 8 /s, 4 kB .. 200 kB)
      bulk_transfer  - few very large transfers  (< 2 /s, > 500 kB)
      streaming      - periodic large segments   (0.2 .. 8 /s, 200 kB .. 5 MB)
      unknown        - anything else
    """
    if rate_per_s <= 0:
        return UNKNOWN
    if rate_per_s >= 20 and mean_payload_bytes < 4_000:
        return "realtime_call"
    if rate_per_s >= 8 and 4_000 <= mean_payload_bytes < 200_000:
        return "inference"
    if rate_per_s < 2 and mean_payload_bytes > 500_000:
        return "bulk_transfer"
    if 0.2 <= rate_per_s < 8 and 200_000 <= mean_payload_bytes <= 5_000_000:
        return "streaming"
    return UNKNOWN


class IntentAgent:
    def __init__(self, default: str = "inference",
                 profile: Optional[str] = None) -> None:
        self.profile = profile
        if profile is not None:
            default = APPLICATION_PROFILES[profile]["intent"]
        self.intent = default
        self.observed = UNKNOWN
        self._rates: Deque[float] = deque(maxlen=20)
        self._sizes: Deque[int] = deque(maxlen=20)

    def observe(self, frames_per_s: float, mean_payload_bytes: int) -> None:
        """Update the advisory classification from the traffic shape."""
        self._rates.append(frames_per_s)
        self._sizes.append(mean_payload_bytes)
        rate = sum(self._rates) / len(self._rates)
        size = sum(self._sizes) / len(self._sizes)
        self.observed = classify_traffic(rate, size)
        if self.profile is None:
            # No explicit profile: use the classification, or the
            # conservative policy when it is uncertain.
            self.intent = (self.observed if self.observed != UNKNOWN
                           else CONSERVATIVE_INTENT)

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
            "inference": "live inspection frames (needs lowest total response time)",
            "realtime_call": "live call (needs steady timing above all)",
            "bulk_transfer": "bulk transfer (needs throughput, tolerates jitter)",
            "streaming": "buffered playback (buffer absorbs short interruptions)",
        }[self.intent]
