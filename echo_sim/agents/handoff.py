"""Handoff Agent - move a live inference session without restarting it.

The whole sequence runs *before* the current path becomes unusable, which is
the difference between this and a reconnect:

    PREPARE  -> target allocates resources (the model is already resident)
    STATE    -> the live session state crosses; only the session, not the model
    READY    -> target confirms it rebuilt the session
    VERIFY   -> target infers one duplicate frame, proving it really works
    COMMIT   -> target becomes the active inference server
    ACK      -> handoff complete
    DRAIN    -> old edge finishes outstanding work and takes no more

The old session is not discarded until the target has acknowledged. If any step
fails, nothing has been lost and the Recovery Agent simply keeps the current
edge.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .. import protocol as P
from .decision import Candidate

STABLE = "STABLE"
PREPARING = "PREPARING"
TRANSFERRING = "TRANSFERRING"
VERIFYING = "VERIFYING"
COMMITTING = "COMMITTING"
DRAINING = "DRAINING"
RECOVERING = "RECOVERING"


@dataclass
class HandoffResult:
    ok: bool
    source_edge: str
    target_edge: str
    target_network: str
    reason: str = ""
    prepare_ms: float = 0.0
    state_ms: float = 0.0
    verify_ms: float = 0.0
    commit_ms: float = 0.0
    total_ms: float = 0.0
    state_bytes: int = 0
    state_version: int = 0
    timings: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "source_edge": self.source_edge,
            "target_edge": self.target_edge,
            "target_network": self.target_network,
            "reason": self.reason,
            "prepare_ms": round(self.prepare_ms, 2),
            "state_ms": round(self.state_ms, 2),
            "verify_ms": round(self.verify_ms, 2),
            "commit_ms": round(self.commit_ms, 2),
            "total_ms": round(self.total_ms, 2),
            "state_bytes": self.state_bytes,
            "state_version": self.state_version,
        }


class HandoffManager:
    """Drives the ECHO sequence over whatever transport the client provides.

    `transport` must offer:
        await open_standby(edge_id, network_id)
        await request_standby(msg, timeout) -> reply | None
        await promote_standby()
        await request_primary(msg, timeout) -> reply | None
        await close_standby()
    """

    def __init__(self, transport, cfg, telemetry=None, explainer=None) -> None:
        self.transport = transport
        self.cfg = cfg
        self.telemetry = telemetry
        self.explainer = explainer
        self.state = STABLE
        self.history: list[HandoffResult] = []

    def _set(self, state: str, **extra) -> None:
        self.state = state
        if self.telemetry:
            self.telemetry.emit("handoff_state", state=state, **extra)

    async def execute(self, source_edge: str, target: Candidate,
                      session_state: P.SessionState) -> HandoffResult:
        t_start = time.perf_counter()
        res = HandoffResult(ok=False, source_edge=source_edge,
                            target_edge=target.edge_id,
                            target_network=target.network_id)
        sid = session_state.session_id
        timeout = 2.0

        try:
            # --- PREPARE ---------------------------------------------------
            self._set(PREPARING, target_edge=target.edge_id,
                      target_network=target.network_id)
            t0 = time.perf_counter()
            await self.transport.open_standby(target.edge_id, target.network_id)
            reply = await self.transport.request_standby(
                P.message(P.ECHO_PREPARE, session_id=sid,
                          model_id=session_state.model_id,
                          model_version=session_state.model_version),
                timeout)
            res.prepare_ms = (time.perf_counter() - t0) * 1000
            if reply is None or reply.get("type") != P.ECHO_ACK:
                res.reason = "prepare failed" if reply is None else reply.get(
                    "reason", "prepare refused")
                return await self._abort(res)

            # --- STATE -> READY --------------------------------------------
            self._set(TRANSFERRING, target_edge=target.edge_id)
            t0 = time.perf_counter()
            res.state_bytes = session_state.size_bytes()
            res.state_version = session_state.state_version
            reply = await self.transport.request_standby(
                P.message(P.ECHO_STATE, session_id=sid,
                          state=session_state.to_dict()), timeout)
            res.state_ms = (time.perf_counter() - t0) * 1000
            if reply is None or reply.get("type") != P.ECHO_READY:
                res.reason = "target never became ready"
                return await self._abort(res)

            # --- VERIFY ----------------------------------------------------
            # A duplicate frame, so a broken target is caught before commit
            # rather than after, while the old edge is still serving.
            self._set(VERIFYING, target_edge=target.edge_id)
            t0 = time.perf_counter()
            reply = await self.transport.request_standby(
                P.message(P.ECHO_VERIFY, session_id=sid,
                          frame_no=session_state.last_processed_frame), timeout)
            res.verify_ms = (time.perf_counter() - t0) * 1000
            if reply is None or reply.get("type") != P.ECHO_RESULT:
                res.reason = "verification frame failed on the target"
                return await self._abort(res)

            # --- COMMIT ----------------------------------------------------
            self._set(COMMITTING, target_edge=target.edge_id)
            t0 = time.perf_counter()
            reply = await self.transport.request_standby(
                P.message(P.ECHO_COMMIT, session_id=sid), timeout)
            res.commit_ms = (time.perf_counter() - t0) * 1000
            if reply is None or reply.get("type") != P.ECHO_ACK:
                res.reason = "commit not acknowledged"
                return await self._abort(res)

            old_primary = source_edge
            await self.transport.promote_standby()

            # --- DRAIN (best effort, old path may already be gone) ----------
            self._set(DRAINING, source_edge=old_primary)
            await self.transport.drain(old_primary, sid)

            res.ok = True
            res.total_ms = (time.perf_counter() - t_start) * 1000
            self._set(STABLE, edge=target.edge_id, network=target.network_id)
            self.history.append(res)
            if self.telemetry:
                self.telemetry.emit("handoff_complete", **res.as_dict())
            return res

        except Exception as exc:  # noqa: BLE001 - a failed handoff must not kill the run
            res.reason = f"{type(exc).__name__}: {exc}"
            return await self._abort(res)

    async def _abort(self, res: HandoffResult) -> HandoffResult:
        res.ok = False
        res.total_ms = res.prepare_ms + res.state_ms + res.verify_ms + res.commit_ms
        self._set(RECOVERING, reason=res.reason)
        await self.transport.close_standby()
        self._set(STABLE)
        self.history.append(res)
        if self.telemetry:
            self.telemetry.emit("handoff_failed", **res.as_dict())
        return res
