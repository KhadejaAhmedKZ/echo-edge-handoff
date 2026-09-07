"""Recovery Agent - make a failed handoff a non-event.

Because the session state is only ever *copied* to the target and the old edge
is not drained until the target acknowledges, a failed handoff costs nothing
but the attempt. This agent decides what to do next:

  * old path still usable  -> stay put, back off before retrying
  * old path gone too      -> cold-start the best remaining reachable edge,
                              which is the one case where a restart is genuine
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .decision import Candidate, DecisionEngine
from .handoff import HandoffResult
from .watcher import Watcher

STAY = "stay"
COLD_START = "cold_start"
RETRY_ELSEWHERE = "retry_elsewhere"


@dataclass
class RecoveryPlan:
    action: str
    target: Optional[Candidate]
    reason: str
    backoff_s: float = 0.0


class RecoveryAgent:
    def __init__(self, watcher: Watcher, decision: DecisionEngine,
                 backoff_s: float = 2.0) -> None:
        self.watcher = watcher
        self.decision = decision
        self.backoff_s = backoff_s
        self.blocked_until: dict[str, float] = {}
        self.recoveries = 0

    def block(self, edge_id: str, seconds: Optional[float] = None) -> None:
        self.blocked_until[edge_id] = time.monotonic() + (seconds or self.backoff_s)

    def is_blocked(self, edge_id: str) -> bool:
        return time.monotonic() < self.blocked_until.get(edge_id, 0.0)

    def blocked_edges(self) -> list[str]:
        return [eid for eid in self.blocked_until if self.is_blocked(eid)]

    def plan(self, failure: HandoffResult, current: Candidate) -> RecoveryPlan:
        self.recoveries += 1
        self.block(failure.target_edge)

        if current.reachable:
            return RecoveryPlan(
                STAY, None,
                f"{failure.target_edge} was not usable ({failure.reason}); "
                f"the session never left {current.edge_id}",
                self.backoff_s)

        for cand in self.decision.rank():
            if not cand.reachable or self.is_blocked(cand.edge_id):
                continue
            if cand.edge_id == failure.target_edge:
                continue
            return RecoveryPlan(
                RETRY_ELSEWHERE, cand,
                f"{failure.target_edge} failed and the old path is gone; "
                f"trying {cand.edge_id} over {cand.network_id} instead")

        return RecoveryPlan(
            COLD_START, None,
            "no edge is reachable; holding the session and retrying")
