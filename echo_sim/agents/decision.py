"""Decision Engine - pick the (network, edge) pair with the lowest predicted cost.

Two things make this more than "choose the lowest ping".

First, it scores the *pair*. An edge is only as good as the path you reach it
over, and a network is only useful for the compute it can get you to, so the
candidate set is every network x every edge, and the winner carries both.

Second, it scores the *predicted* state, not the current one, and it scores
total end-to-end inference cost rather than network latency alone. That is why
satellite loses here even when it is the only network with full coverage: it is
available, and it is still the wrong answer.

Hysteresis is what keeps it from oscillating: a challenger has to be better by
a margin, for several consecutive ticks, before a handoff is authorised.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..config import EDGE_ORDER, EDGES, NETWORK_ORDER, Weights, backhaul_ms
from .intent import IntentAgent
from .predictor import Predictor
from .watcher import Watcher


@dataclass
class Candidate:
    edge_id: str
    network_id: str
    score: float
    path_rtt_ms: float
    jitter_ms: float
    loss: float
    inference_ms: float
    cpu: float
    e2e_ms: float
    terms: Dict[str, float] = field(default_factory=dict)
    reachable: bool = True

    def as_dict(self) -> dict:
        import math

        def num(x: float, nd: int = 2):
            return None if (math.isinf(x) or math.isnan(x)) else round(x, nd)

        return {
            "edge_id": self.edge_id,
            "network_id": self.network_id,
            "score": num(self.score),
            "path_rtt_ms": num(self.path_rtt_ms, 1),
            "jitter_ms": num(self.jitter_ms),
            "loss": num(self.loss, 4),
            "inference_ms": num(self.inference_ms, 1),
            "cpu": num(self.cpu, 3),
            "e2e_ms": num(self.e2e_ms, 1),
            "terms": {k: num(v) for k, v in self.terms.items()},
            "reachable": self.reachable,
        }


class DecisionEngine:
    def __init__(self, watcher: Watcher, predictor: Predictor, intent: IntentAgent,
                 user_position, margin: float = 0.12, patience: int = 3, stable_link_hold_s: float = 0.0) -> None:
        self.watcher = watcher
        self.predictor = predictor
        self.intent = intent
        self.user_position = user_position   # callable -> 0..1
        self.margin = margin
        self.patience = patience
        self._streak: Dict[Tuple[str, str], int] = {}
        self._better_since = {}
        self.stable_link_hold_s = stable_link_hold_s
        self.last_ranking: List[Candidate] = []
        # Anti ping-pong: going straight back to the server just left needs
        # stronger and longer evidence than an ordinary switch.
        self.return_window_s = 10.0
        self.return_margin = 0.25
        self.return_patience_factor = 3
        self._left: Optional[Tuple[str, float]] = None
        # No discretionary move until the Watcher has this many samples.
        self.min_history = 8

    def note_left(self, edge_id: str) -> None:
        """Called when the session leaves `edge_id`."""
        self._left = (edge_id, time.monotonic())

    def _returning(self, edge_id: str) -> bool:
        return (self._left is not None and self._left[0] == edge_id
                and time.monotonic() - self._left[1] < self.return_window_s)

    # -- scoring -----------------------------------------------------------

    def score_pair(self, network_id: str, edge_id: str, w: Weights,
                   hosting: Optional[str] = None) -> Candidate:
        np_ = self.predictor.network(network_id)
        edge = EDGES[edge_id]
        health = self.watcher.latest_edges.get(edge_id, {})
        infer = self.predictor.edge_inference_ms(edge_id)
        cpu = health.get("cpu", 0.3)
        # Score every server as if it hosted THIS session. The server that
        # already hosts it carries its load in the measurement; any other
        # server would gain it by moving. Without this, the robot's own session
        # makes whichever server it is on look worse, and the controller
        # ping-pongs between two servers (fixed 2026-09-23).
        if edge_id != hosting:
            infer += health.get("load_penalty_ms", 0.0)
            cpu = min(0.99, cpu + health.get("session_cpu_cost", 0.0))

        # Reachable needs BOTH a usable smoothed level and a live measurement:
        # a smoothed level alone lags an abrupt outage by seconds.
        latest = self.watcher.latest_networks.get(network_id)
        reachable = np_["quality"] > 0.02 and (latest is None or latest.available)
        path_rtt = np_["rtt_ms"] + 2 * backhaul_ms(network_id, edge_id)
        proximity = abs(edge.site_position - self.user_position())

        terms = {
            "network_latency": w.network_latency * path_rtt,
            "jitter": w.jitter * np_["jitter_ms"],
            "packet_loss": w.packet_loss * np_["loss"],
            "inference_latency": w.inference_latency * infer,
            "edge_load": w.edge_load * (cpu * 10.0),
            "proximity": w.proximity * proximity,
        }
        score = sum(terms.values())
        if not reachable:
            score = float("inf")

        # What the user would actually feel: round trip + compute + loss retries.
        e2e = path_rtt + infer + np_["loss"] * path_rtt * 2.0

        return Candidate(edge_id, network_id, score, path_rtt, np_["jitter_ms"],
                         np_["loss"], infer, cpu, e2e, terms, reachable)

    def rank(self, hosting: Optional[str] = None) -> List[Candidate]:
        w = self.intent.weights
        cands = [self.score_pair(n, e, w, hosting)
                 for e in EDGE_ORDER for n in NETWORK_ORDER]
        cands.sort(key=lambda c: c.score)
        self.last_ranking = cands
        return cands

    def best(self, hosting: Optional[str] = None) -> Optional[Candidate]:
        ranked = [c for c in self.rank(hosting) if c.reachable]
        return ranked[0] if ranked else None

    def current_candidate(self, network_id: str, edge_id: str) -> Candidate:
        return self.score_pair(network_id, edge_id, self.intent.weights,
                               hosting=edge_id)

    @property
    def streak_view(self) -> dict:
        """How close each challenger is to earning a handoff, for the dashboard."""
        return {f"{e}/{n}": v for (n, e), v in self._streak.items() if v}

    def reset_evidence(self) -> None:
        self._streak.clear()
        self._better_since.clear()

    # -- should we move? ---------------------------------------------------

    def evaluate(self, current_network: str, current_edge: str
                 ) -> Tuple[Optional[Candidate], Optional[Candidate], str]:
        """Return (current, chosen_challenger_or_None, reason)."""
        current = self.current_candidate(current_network, current_edge)
        best = self.best(hosting=current_edge)
        if best is None:
            return current, None, "no reachable edge"

        # Current path is gone: move immediately, hysteresis does not apply.
        if not current.reachable:
            self.reset_evidence()
            return current, best, "current path unreachable"

        if best.edge_id == current.edge_id and best.network_id == current.network_id:
            self.reset_evidence()
            return current, None, "already on the best server and network"

        history = len(self.watcher.net_history[current.network_id]["quality"])
        if history < self.min_history:
            return current, None, (
                f"still learning the links ({history} of {self.min_history} "
                f"measurements); not switching on so little evidence")

        key = (best.network_id, best.edge_id)
        # Evidence must be consecutive for the SAME challenger. Alternating
        # winners must never accumulate old votes toward a later switch.
        for old in list(self._streak):
            if old != key:
                self._streak.pop(old, None)
                self._better_since.pop(old, None)
        margin, patience = self.margin, self.patience
        returning = best.edge_id != current.edge_id and self._returning(best.edge_id)
        if returning:
            margin = max(margin, self.return_margin)
            patience = patience * self.return_patience_factor
        improvement = (current.score - best.score) / max(current.score, 1e-6)
        if improvement < margin:
            self.reset_evidence()
            return current, None, (
                f"Server {best.edge_id} over {best.network_id} is only "
                f"{improvement * 100:.0f}% better, below the "
                f"{margin * 100:.0f}% switching margin"
                + (" for returning to a server just left" if returning else ""))

        # With strong, stable coverage, a delay spike is not yet evidence of
        # leaving the site. Require sustained benefit; use raw measurements
        # to cancel stale smoothed/forecast advantages after recovery.
        latest = self.watcher.latest_networks.get(current_network)
        qhist = list(self.watcher.net_history[current_network]["quality"])[-8:]
        stable_coverage = (self.stable_link_hold_s > 0 and latest is not None and latest.quality >= 0.65
                           and len(qhist) >= 4)
        if stable_coverage:
            live_best = self.watcher.latest_networks.get(best.network_id)
            def live_cost(n, e, prof):
                h = self.watcher.latest_edges.get(e, {})
                infer = h.get("expected_inference_ms", 0.0)
                if e != current_edge:
                    infer += h.get("load_penalty_ms", 0.0)
                return prof.rtt_ms + 2 * backhaul_ms(n, e) + infer + 300 * prof.loss
            if live_best is None or (live_cost(current_network, current_edge, latest)
                    <= live_cost(best.network_id, best.edge_id, live_best)):
                self.reset_evidence()
                return current, None, "current measurements no longer justify a switch; letting the short disturbance settle"
        now = time.monotonic()
        since = self._better_since.setdefault(key, now)
        self._streak[key] = self._streak.get(key, 0) + 1
        if self._streak[key] < patience:
            return current, None, (
                f"Server {best.edge_id} over {best.network_id} has been better "
                f"for {self._streak[key]} of {patience} required checks"
                + (" (stricter: it was just left)" if returning else ""))

        if stable_coverage and now - since < self.stable_link_hold_s:
            return current, None, (
                f"coverage is still strong; checking that the improvement lasts "
                f"({now - since:.1f} of {self.stable_link_hold_s:.1f} seconds)")
        self.reset_evidence()
        return current, best, (
            f"Server {best.edge_id} over {best.network_id} is "
            f"{improvement * 100:.0f}% better and has held that for "
            f"{patience} consecutive checks")
