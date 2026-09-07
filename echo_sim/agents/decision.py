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
                 user_position, margin: float = 0.12, patience: int = 3) -> None:
        self.watcher = watcher
        self.predictor = predictor
        self.intent = intent
        self.user_position = user_position   # callable -> 0..1
        self.margin = margin
        self.patience = patience
        self._streak: Dict[Tuple[str, str], int] = {}
        self.last_ranking: List[Candidate] = []

    # -- scoring -----------------------------------------------------------

    def score_pair(self, network_id: str, edge_id: str, w: Weights) -> Candidate:
        np_ = self.predictor.network(network_id)
        edge = EDGES[edge_id]
        health = self.watcher.latest_edges.get(edge_id, {})
        infer = self.predictor.edge_inference_ms(edge_id)
        cpu = health.get("cpu", 0.3)

        reachable = np_["quality"] > 0.02
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

    def rank(self) -> List[Candidate]:
        w = self.intent.weights
        cands = [self.score_pair(n, e, w)
                 for e in EDGE_ORDER for n in NETWORK_ORDER]
        cands.sort(key=lambda c: c.score)
        self.last_ranking = cands
        return cands

    def best(self) -> Optional[Candidate]:
        ranked = [c for c in self.rank() if c.reachable]
        return ranked[0] if ranked else None

    def current_candidate(self, network_id: str, edge_id: str) -> Candidate:
        return self.score_pair(network_id, edge_id, self.intent.weights)

    # -- should we move? ---------------------------------------------------

    def evaluate(self, current_network: str, current_edge: str
                 ) -> Tuple[Optional[Candidate], Optional[Candidate], str]:
        """Return (current, chosen_challenger_or_None, reason)."""
        current = self.current_candidate(current_network, current_edge)
        best = self.best()
        if best is None:
            return current, None, "no reachable edge"

        # Current path is gone: move immediately, hysteresis does not apply.
        if not current.reachable:
            self._streak.clear()
            return current, best, "current path unreachable"

        if best.edge_id == current.edge_id and best.network_id == current.network_id:
            self._streak.clear()
            return current, None, "already on the best pair"

        key = (best.network_id, best.edge_id)
        improvement = (current.score - best.score) / max(current.score, 1e-6)
        if improvement < self.margin:
            self._streak[key] = 0
            return current, None, (
                f"{best.edge_id} is only {improvement * 100:.0f}% better, "
                f"below the {self.margin * 100:.0f}% switching margin")

        self._streak[key] = self._streak.get(key, 0) + 1
        if self._streak[key] < self.patience:
            return current, None, (
                f"{best.edge_id} has been better for {self._streak[key]} of "
                f"{self.patience} required ticks")

        self._streak.clear()
        return current, best, (
            f"{best.edge_id} is {improvement * 100:.0f}% better and has held that "
            f"for {self.patience} consecutive ticks")
