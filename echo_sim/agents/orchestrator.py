"""Orchestrator - runs the agent cycle and decides what actually happens.

The Decision Engine answers "which pair is best?". That is not the same
question as "should we move, right now?", and conflating the two is how a
system ends up thrashing: acting on a correct ranking at the wrong moment.

So the Orchestrator sits between the Decision Engine and the Handoff Manager.
It takes the recommendation and applies the guards that only make sense with a
view of the whole system:

* a handoff is already in flight  -> hold, one migration at a time
* we just finished one           -> hold, cooldown before another
* Recovery has backed that edge off -> hold, it failed recently
* only the path differs, not the compute -> downgrade to a path migration,
  which QUIC does for free and which needs no session work at all

It also chooses the *cheapest action that achieves the goal*, which is the part
that matters: moving a compute session is expensive, and most improvements do
not need one.

The baselines have no orchestration on purpose. TCP and QUIC-only are supposed
to show what happens without it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Tuple

from ..config import NETWORK_ORDER
from .decision import Candidate, DecisionEngine
from .handoff import STABLE, HandoffManager
from .intent import IntentAgent
from .recovery import RecoveryAgent

# actions the Orchestrator can choose
HOLD = "hold"
MIGRATE_PATH = "migrate_path"
HANDOFF = "handoff"


@dataclass
class Action:
    kind: str
    target: Optional[Candidate]
    reason: str
    veto: str = ""

    @property
    def is_move(self) -> bool:
        return self.kind in (MIGRATE_PATH, HANDOFF)


class Orchestrator:
    def __init__(self, decision: DecisionEngine, handoff: HandoffManager,
                 recovery: RecoveryAgent, intent: IntentAgent,
                 cooldown_s: float = 3.0, buffer_provider=None,
                 buffer_hold_s: float = 4.0) -> None:
        self.decision = decision
        # Buffered playback: seconds of upcoming content already held, or None
        # for live traffic that cannot be buffered.
        self.buffer_provider = buffer_provider or (lambda: None)
        self.buffer_hold_s = buffer_hold_s
        self.handoff = handoff
        self.recovery = recovery
        self.intent = intent
        self.cooldown_s = cooldown_s

        self.cycles = 0
        self.moves = 0
        self.vetoes = 0
        self.last_action = HOLD
        self.last_reason = "not started"
        self.last_veto = ""
        self._last_move_ts = 0.0

    # -- helpers -----------------------------------------------------------

    def note_move(self) -> None:
        """Called once a move actually completes, to start the cooldown."""
        self._last_move_ts = time.monotonic()
        self.moves += 1

    @property
    def cooldown_left_s(self) -> float:
        return max(0.0, self.cooldown_s - (time.monotonic() - self._last_move_ts))

    def _hold(self, reason: str, veto: str = "") -> Action:
        self.last_action, self.last_reason, self.last_veto = HOLD, reason, veto
        if veto:
            self.vetoes += 1
        return Action(HOLD, None, reason, veto)

    # -- the cycle ---------------------------------------------------------

    def plan(self, current_network: str, current_edge: str
             ) -> Tuple[Action, Candidate]:
        """Decide what to do this tick. Returns (action, current candidate)."""
        self.cycles += 1
        current, challenger, reason = self.decision.evaluate(
            current_network, current_edge)

        # One migration at a time. A second one on top of an in-flight handoff
        # would leave two edges thinking they own the session.
        if self.handoff.state != STABLE:
            return self._hold(f"a handoff is already in flight "
                              f"({self.handoff.state.lower()})"), current

        if challenger is None:
            return self._hold(reason), current

        # The current network is gone. The cheapest action that restores
        # service is to move the connection (QUIC migration, no session work)
        # to the best network that still reaches the CURRENT server. A server
        # change is then considered normally, make-before-break, while this
        # path serves - instead of running a whole handover blind.
        if not current.reachable and challenger.edge_id != current_edge:
            w = self.decision.intent.weights
            paths = [self.decision.score_pair(n, current_edge, w, hosting=current_edge)
                     for n in NETWORK_ORDER if n != current_network]
            paths = [p for p in paths if p.reachable]
            if paths:
                bridge = min(paths, key=lambda p: p.score)
                self.last_action, self.last_reason, self.last_veto = (
                    MIGRATE_PATH, "current network lost", "")
                return Action(MIGRATE_PATH, bridge,
                              f"current network lost; moving the connection to "
                              f"{bridge.network_id} at once to keep Server "
                              f"{current_edge} serving, before considering "
                              f"Server {challenger.edge_id}"), current

        # An edge that just failed us does not get another go immediately.
        if self.recovery.is_blocked(challenger.edge_id):
            return self._hold(
                f"Server {challenger.edge_id} looks best, but it failed recently and is "
                f"backed off",
                veto=f"blocked:{challenger.edge_id}"), current

        # Cooldown, unless the current path is actually gone - in which case
        # waiting is worse than moving.
        if self.cooldown_left_s > 0 and current.reachable:
            return self._hold(
                f"Server {challenger.edge_id} looks better, but the last move was "
                f"{self.cooldown_s - self.cooldown_left_s:.1f}s ago",
                veto="cooldown"), current

        # Buffered playback: if the buffer already covers far more than a
        # disturbance usually lasts, and the current path still delivers,
        # moving is not worth its cost. Live traffic has no buffer to lean on.
        buffered = self.buffer_provider()
        if (buffered is not None and current.reachable
                and buffered >= self.buffer_hold_s):
            return self._hold(
                f"Server {challenger.edge_id} looks better, but {buffered:.1f} s of "
                f"upcoming content is already buffered and the current path "
                f"still delivers", veto="buffer_covers"), current

        # Cheapest sufficient action: if the compute is already in the right
        # place, this is a path change, not a session migration.
        if challenger.edge_id == current_edge:
            self.last_action, self.last_reason, self.last_veto = (
                MIGRATE_PATH, reason, "")
            return Action(MIGRATE_PATH, challenger,
                          f"same server, better network - moving the connection "
                          f"instead of the session. {reason}"), current

        self.last_action, self.last_reason, self.last_veto = HANDOFF, reason, ""
        return Action(HANDOFF, challenger, reason), current

    # -- reporting ---------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "cycles": self.cycles,
            "moves": self.moves,
            "vetoes": self.vetoes,
            "last_action": self.last_action,
            "last_reason": self.last_reason,
            "last_veto": self.last_veto,
            "cooldown_s": self.cooldown_s,
            "cooldown_left_s": round(self.cooldown_left_s, 1),
        }
