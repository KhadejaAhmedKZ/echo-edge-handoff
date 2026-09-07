"""Explainer - turn every decision into a sentence a person would accept.

An automatic switch with no explanation reads as a glitch. The same switch with
a reason reads as the system working. Everything below is generated from the
same numbers the Decision Engine used, so the explanation cannot drift away
from the actual reason.
"""
from __future__ import annotations

from ..config import EDGES, NETWORKS
from .decision import Candidate


def _net(nid: str) -> str:
    return NETWORKS[nid].label if nid in NETWORKS else nid


def _edge(eid: str) -> str:
    return EDGES[eid].label if eid in EDGES else eid


class Explainer:
    def why_switch(self, current: Candidate, target: Candidate, reason: str) -> str:
        bits = []
        if target.network_id != current.network_id:
            bits.append(f"moving from {_net(current.network_id)} to "
                        f"{_net(target.network_id)}")
        if target.edge_id != current.edge_id:
            bits.append(f"moving inference from {_edge(current.edge_id)} to "
                        f"{_edge(target.edge_id)}")
        head = " and ".join(bits) if bits else "adjusting the path"

        delta = current.e2e_ms - target.e2e_ms
        return (f"{head.capitalize()}. Predicted end-to-end inference goes from "
                f"{current.e2e_ms:.0f} ms to {target.e2e_ms:.0f} ms "
                f"({delta:+.0f} ms). {reason.capitalize()}.")

    def why_stay(self, current: Candidate, reason: str) -> str:
        return (f"Staying on {_edge(current.edge_id)} over {_net(current.network_id)} "
                f"at {current.e2e_ms:.0f} ms end-to-end. {reason.capitalize()}.")

    def preparing(self, target: Candidate) -> str:
        return (f"{_net(target.network_id)} is improving and "
                f"{_edge(target.edge_id)} is predicted to give lower end-to-end "
                f"inference. Preparing {_edge(target.edge_id)} now, before the "
                f"current path degrades.")

    def committed(self, source_edge: str, target_edge: str, state_version: int,
                  handoff_ms: float) -> str:
        return (f"Inference session moved from {_edge(source_edge)} to "
                f"{_edge(target_edge)}. State version {state_version} transferred "
                f"and verified; handoff took {handoff_ms:.0f} ms with no session "
                f"restart.")

    def recovered(self, failed_edge: str, fallback_edge: str, reason: str) -> str:
        return (f"Handoff to {_edge(failed_edge)} did not complete ({reason}). "
                f"Falling back to {_edge(fallback_edge)}; the session was never "
                f"discarded, so nothing had to be rebuilt.")

    def trouble(self, diagnosis: dict) -> str:
        head = diagnosis["headline"]
        detail = diagnosis["detail"]
        if diagnosis["cause"] == "healthy":
            return f"Running normally at {diagnosis['e2e_ms']:.0f} ms end-to-end."
        sentence = f"Things feel slow because {head}"
        if detail:
            sentence += f" - {detail}"
        return sentence + "."
