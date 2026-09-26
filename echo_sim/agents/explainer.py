"""Explainer - turn every decision into a sentence a person would accept.

Sentences are generated deterministically from the same numbers the Decision
Engine used, so the explanation cannot drift away from the actual reason. No
language model is involved; an optional rephrasing step could be added later,
but it must never invent reasons, delay a switch or be needed for judging.
"""
from __future__ import annotations

from ..config import EDGES, NETWORKS
from .decision import Candidate


def _net(nid: str) -> str:
    return NETWORKS[nid].label if nid in NETWORKS else nid


def _srv(eid: str) -> str:
    return f"Server {eid}" if eid in EDGES else eid


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


class Explainer:
    def why_switch(self, current: Candidate, target: Candidate, reason: str) -> str:
        return (f"Switching to {_srv(target.edge_id)} over {_net(target.network_id)}: "
                f"expected response time {target.e2e_ms:.0f} ms instead of "
                f"{current.e2e_ms:.0f} ms, and the improvement has persisted. "
                f"{_cap(reason)}.")

    def why_stay(self, current: Candidate, reason: str) -> str:
        return (f"Staying on {_srv(current.edge_id)} over {_net(current.network_id)}; "
                f"expected response time {current.e2e_ms:.0f} ms. {_cap(reason)}.")

    def preparing(self, current: Candidate, target: Candidate) -> str:
        return (f"Preparing {_srv(target.edge_id)} over {_net(target.network_id)}: "
                f"expected response time {target.e2e_ms:.0f} ms instead of "
                f"{current.e2e_ms:.0f} ms, and the improvement has persisted. "
                f"{_srv(current.edge_id)} keeps serving until the target is checked.")

    def committed(self, source_edge: str, target_edge: str, state_version: int,
                  handoff_ms: float) -> str:
        return (f"The server changed from {_srv(source_edge)} to {_srv(target_edge)}. "
                f"The session identity and progress continued (state version "
                f"{state_version} transferred and checked); the switch took "
                f"{handoff_ms:.0f} ms with no session restart.")

    def recovered(self, failed_edge: str, fallback_edge: str, reason: str) -> str:
        return (f"{_srv(failed_edge)} could not take the session ({reason}). "
                f"Staying on {_srv(fallback_edge)}, which never stopped serving; "
                f"nothing had to be rebuilt.")

    def trouble(self, diagnosis: dict) -> str:
        head = diagnosis["headline"]
        detail = diagnosis["detail"]
        if diagnosis["cause"] == "healthy":
            return f"Running normally; expected response time {diagnosis['e2e_ms']:.0f} ms."
        sentence = f"Responses are slower because {head}"
        if detail:
            sentence += f" - {detail}"
        return sentence + "."
