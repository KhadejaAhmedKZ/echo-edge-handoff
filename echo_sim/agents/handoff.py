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

# Plain-language step names shown on the main screen; the technical names stay
# available in the payload for anyone who expands the detail.
PHASE_NAMES = {
    STABLE: "idle",
    PREPARING: "prepare",
    TRANSFERRING: "transfer",
    VERIFYING: "check",
    COMMITTING: "switch",
    DRAINING: "finish_old_work",
    RECOVERING: "recovering",
}

CONTINUITY_PROPERTY_READY = (
    "target reports the same session_id, state_version and last completed "
    "request that were transferred")


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
    failed_phase: str = ""
    session_id: str = ""
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
            "failed_phase": self.failed_phase,
            "session_id": self.session_id,
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
        # Set by the controller: highest request number already sent, so the
        # old server knows which outstanding work it should still finish.
        self.last_sent_fn = lambda: None

    def _set(self, state: str, **extra) -> None:
        self.state = state
        if self.telemetry:
            self.telemetry.emit("handoff_phase_changed", "handoff",
                                phase=PHASE_NAMES[state], technical_phase=state,
                                **extra)

    async def execute(self, source_edge: str, target: Candidate,
                      session_state: P.SessionState) -> HandoffResult:
        t_start = time.perf_counter()
        self.last_ready_check = None
        res = HandoffResult(ok=False, source_edge=source_edge,
                            target_edge=target.edge_id,
                            target_network=target.network_id,
                            session_id=session_state.session_id)
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
                res.failed_phase = "prepare"
                return await self._abort(res)

            # --- STATE -> READY --------------------------------------------
            self._set(TRANSFERRING, target_edge=target.edge_id)
            t0 = time.perf_counter()
            # Snapshot, so frames completing during the transfer cannot make
            # the check below compare against a moving target.
            snapshot = session_state.to_dict()
            res.state_bytes = session_state.size_bytes()
            res.state_version = snapshot["state_version"]
            reply = await self.transport.request_standby(
                P.message(P.ECHO_STATE, session_id=sid, state=snapshot), timeout)
            res.state_ms = (time.perf_counter() - t0) * 1000
            if reply is None or reply.get("type") != P.ECHO_READY:
                res.reason = "target never became ready"
                res.failed_phase = "transfer"
                return await self._abort(res)

            # Continuity assertion #1, before anything is committed.
            checks = {
                "session_id": reply.get("session_id") == sid,
                "state_version": reply.get("state_version") == snapshot["state_version"],
                "last_completed_request": (reply.get("last_processed_frame")
                                           == snapshot["last_processed_frame"]),
            }
            ok = all(checks.values())
            self.last_ready_check = {
                "ok": ok, "stage": "before_switch",
                "property": CONTINUITY_PROPERTY_READY, "server": target.edge_id,
                "checks": checks}
            if self.telemetry:
                self.telemetry.emit(
                    "state_verified", "handoff", ok=ok, stage="before_switch",
                    property=CONTINUITY_PROPERTY_READY, session_id=sid,
                    server=target.edge_id, checks=checks,
                    transferred_state_version=snapshot["state_version"],
                    reported_state_version=reply.get("state_version"),
                    last_completed_request=snapshot["last_processed_frame"],
                    payload_bytes=res.state_bytes)
            if not ok:
                res.reason = "target state did not match what was transferred"
                res.failed_phase = "transfer"
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
                res.failed_phase = "check"
                return await self._abort(res)

            # --- COMMIT ----------------------------------------------------
            self._set(COMMITTING, target_edge=target.edge_id)
            t0 = time.perf_counter()
            reply = await self.transport.request_standby(
                P.message(P.ECHO_COMMIT, session_id=sid), timeout)
            res.commit_ms = (time.perf_counter() - t0) * 1000
            if reply is None or reply.get("type") != P.ECHO_ACK:
                res.reason = "commit not acknowledged"
                res.failed_phase = "switch"
                return await self._abort(res)

            old_primary = source_edge
            await self.transport.promote_standby()

            # --- DRAIN (best effort, old path may already be gone) ----------
            last_sent = self.last_sent_fn()
            self._set(DRAINING, source_edge=old_primary, finish_up_to_request=last_sent)
            await self.transport.drain(old_primary, sid, last_sent)

            res.ok = True
            res.total_ms = (time.perf_counter() - t_start) * 1000
            self._set(STABLE, edge=target.edge_id, network=target.network_id)
            self.history.append(res)
            if self.telemetry:
                d = res.as_dict()
                self.telemetry.emit("handoff_completed", "handoff",
                                    source_server=d.pop("source_edge"),
                                    target_server=d.pop("target_edge"), **d)
            return res

        except Exception as exc:  # noqa: BLE001 - a failed handoff must not kill the run
            res.reason = f"{type(exc).__name__}: {exc}"
            res.failed_phase = res.failed_phase or PHASE_NAMES.get(self.state, "")
            return await self._abort(res)

    async def _abort(self, res: HandoffResult) -> HandoffResult:
        res.ok = False
        res.total_ms = res.prepare_ms + res.state_ms + res.verify_ms + res.commit_ms
        self._set(RECOVERING, reason=res.reason, failed_phase=res.failed_phase)
        await self.transport.close_standby()
        self._set(STABLE)
        self.history.append(res)
        if self.telemetry:
            d = res.as_dict()
            self.telemetry.emit("handoff_failed", "handoff",
                                source_server=d.pop("source_edge"),
                                target_server=d.pop("target_edge"), **d)
        return res
