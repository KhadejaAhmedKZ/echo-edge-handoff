"""Watcher - measures every network and every edge, continuously.

The point of this agent is that it does not only look at what is currently in
use. All four access networks and all four edge sites are sampled on every
tick, including the ones nobody is talking to, because you cannot switch to a
network you were not already watching.

Network characteristics come from the link layer (the World, which is the
software equivalent of reading radio/link statistics from the OS). Edge health
comes from the edge's own ECHO_METRICS report.
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Dict, List

from ..config import EDGE_ORDER, EDGES, NETWORK_ORDER, backhaul_ms
from ..world import LiveProfile, World

WINDOW = 40  # ~10 s of history at a 0.25 s tick


class Sample:
    __slots__ = ("t", "value")

    def __init__(self, t: float, value: float) -> None:
        self.t = t
        self.value = value


class Watcher:
    def __init__(self, world: World, edge_services: Dict[str, object]) -> None:
        self.world = world
        self.edges = edge_services
        self.net_history: Dict[str, Dict[str, Deque[Sample]]] = {
            nid: {k: deque(maxlen=WINDOW)
                  for k in ("rtt_ms", "jitter_ms", "loss", "bandwidth_mbps", "quality")}
            for nid in NETWORK_ORDER
        }
        self.edge_history: Dict[str, Deque[Sample]] = {
            eid: deque(maxlen=WINDOW) for eid in EDGE_ORDER
        }
        self.latest_networks: Dict[str, LiveProfile] = {}
        self.latest_edges: Dict[str, dict] = {}

    def tick(self, t: float) -> None:
        self.latest_networks = self.world.all_profiles()
        for nid, prof in self.latest_networks.items():
            h = self.net_history[nid]
            if prof.available:
                h["rtt_ms"].append(Sample(t, prof.rtt_ms))
                h["jitter_ms"].append(Sample(t, prof.jitter_ms))
                h["loss"].append(Sample(t, prof.loss))
                h["bandwidth_mbps"].append(Sample(t, prof.bandwidth_mbps))
            h["quality"].append(Sample(t, prof.quality))

        for eid, svc in self.edges.items():
            health = svc.health()
            self.latest_edges[eid] = health
            self.edge_history[eid].append(Sample(t, health["expected_inference_ms"]))

    # -- derived views -----------------------------------------------------

    def reachable_edges(self, access_network: str | None = None) -> List[str]:
        """Edges we could actually talk to right now, over any live network."""
        live = [nid for nid, p in self.latest_networks.items() if p.available]
        if not live:
            return []
        return [eid for eid in EDGE_ORDER
                if self.latest_edges.get(eid, {}).get("available", False)]

    def best_network_for(self, edge_id: str) -> str | None:
        """Cheapest access network from which to reach this edge right now."""
        best, best_cost = None, float("inf")
        for nid, prof in self.latest_networks.items():
            if not prof.available:
                continue
            cost = prof.rtt_ms + 2 * backhaul_ms(nid, edge_id)
            if cost < best_cost:
                best, best_cost = nid, cost
        return best

    def path_rtt_ms(self, network_id: str, edge_id: str) -> float:
        prof = self.latest_networks.get(network_id)
        if prof is None or not prof.available:
            return float("inf")
        return prof.rtt_ms + 2 * backhaul_ms(network_id, edge_id)

    def snapshot(self) -> dict:
        return {
            "networks": {nid: p.as_dict() for nid, p in self.latest_networks.items()},
            "edges": dict(self.latest_edges),
        }
