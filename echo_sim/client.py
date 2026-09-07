"""Robot client and the controller that wires the agents to the transport.

One process plays the inspection robot: it emits frames at a fixed rate, sends
each one to whichever edge is currently active, and records what came back and
how long it took. Alongside it, the agent loop runs on its own tick, watching
every network and every edge and deciding whether the session should move.

Three modes share every line of this file except the decision branch, which is
the only honest way to compare them:

  tcp   - no migration, no prediction. React after the break, reconnect, and
          rebuild the inference session from nothing.
  quic  - the connection survives a network change, but the compute never
          moves. Shows that transport continuity alone is not the answer.
  echo  - watch, predict, prepare, transfer, verify, commit, drain.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Dict, List, Optional

from . import protocol as P
from .agents.decision import Candidate, DecisionEngine
from .agents.explainer import Explainer
from .agents.handoff import HandoffManager
from .agents.intent import IntentAgent
from .agents.predictor import Predictor
from .agents.recovery import COLD_START, RETRY_ELSEWHERE, RecoveryAgent
from .agents.troubleshooter import Troubleshooter
from .agents.watcher import Watcher
from .config import EDGES, RunConfig
from .metrics import FrameRecord, RunMetrics
from .telemetry import Telemetry
from .world import World

FRAME_PAYLOAD_BYTES = 24_000     # a modest JPEG from an inspection camera
MAX_IN_FLIGHT = 48


class EchoController:
    def __init__(self, cfg: RunConfig, world: World, edge_services: Dict[str, Any],
                 transport, telemetry: Telemetry) -> None:
        self.cfg = cfg
        self.world = world
        self.edges = edge_services
        self.transport = transport
        self.telemetry = telemetry

        self.watcher = Watcher(world, edge_services)
        self.predictor = Predictor(self.watcher, cfg.prediction_horizon_s)
        self.intent = IntentAgent("inference")
        self.decision = DecisionEngine(self.watcher, self.predictor, self.intent,
                                       lambda: world.position,
                                       cfg.switch_margin, cfg.switch_patience)
        self.troubleshooter = Troubleshooter(self.watcher, self.predictor)
        self.explainer = Explainer()
        self.handoff = HandoffManager(transport, cfg, telemetry, self.explainer)
        self.recovery = RecoveryAgent(self.watcher, self.decision)

        self.metrics = RunMetrics(mode=cfg.mode)
        self.state = P.SessionState(session_id=cfg.session_id,
                                    model_id=cfg.model_id,
                                    model_version=cfg.model_version)
        self.frame_no = 1000
        self.in_flight = 0
        self.consecutive_failures = 0
        self.pinned_edge: Optional[str] = None      # quic-only mode stays here
        self.last_explanation = ""
        self.last_diagnosis: Dict[str, Any] = {}
        self.last_reason = ""
        self.last_handoff_ms: Optional[float] = None
        self.running = False
        self._t0 = time.monotonic()
        self._sent_recent: List[float] = []

    # -- helpers -----------------------------------------------------------

    def now(self) -> float:
        return time.monotonic() - self._t0

    def current_pair(self) -> tuple[str, str]:
        return (self.transport.network_id or "wifi",
                self.transport.edge_id or "A")

    def _explain(self, text: str, **extra) -> None:
        if text and text != self.last_explanation:
            self.last_explanation = text
            self.telemetry.emit("explanation", text=text, **extra)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> bool:
        self.watcher.tick(self.now())
        best = self.decision.best()
        if best is None:
            return False
        ok = await self.transport.open_primary(best.edge_id, best.network_id)
        if not ok:
            return False
        reply = await self.transport.request_primary(
            P.message(P.ECHO_INIT, session_id=self.cfg.session_id,
                      model_id=self.cfg.model_id,
                      model_version=self.cfg.model_version), 5.0)
        if reply is None:
            return False
        if self.cfg.mode == "quic":
            self.pinned_edge = best.edge_id
        self.telemetry.emit("session_started", edge_id=best.edge_id,
                            network_id=best.network_id, mode=self.cfg.mode)
        self._explain(
            f"Session started on {EDGES[best.edge_id].label} over "
            f"{best.network_id}, predicted {best.e2e_ms:.0f} ms end to end.")
        return True

    async def run(self) -> RunMetrics:
        self.running = True
        tasks = [asyncio.ensure_future(self._frame_loop()),
                 asyncio.ensure_future(self._agent_loop())]
        try:
            await asyncio.sleep(self.cfg.duration_s)
        finally:
            self.running = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0.3)   # let the last few frames land
        return self.metrics

    # -- frame path --------------------------------------------------------

    async def _frame_loop(self) -> None:
        interval = self.cfg.frame_interval_s
        next_at = time.monotonic()
        pending: set = set()
        while self.running:
            next_at += interval
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))
            self.frame_no += 1
            if self.in_flight >= MAX_IN_FLIGHT:
                # Backpressure: the path cannot keep up, so this frame is lost
                # rather than queued behind a queue that is already too long.
                rec = FrameRecord(self.frame_no, self.now(), None, None, None,
                                  self.transport.edge_id, self.transport.network_id,
                                  lost=True)
                self.metrics.add(rec)
                self.telemetry.emit("frame", **rec.as_dict())
                continue
            task = asyncio.ensure_future(self._send_frame(self.frame_no))
            pending.add(task)
            task.add_done_callback(pending.discard)
        for t in list(pending):
            t.cancel()

    async def _send_frame(self, frame_no: int) -> None:
        sent_t = self.now()
        edge_id = self.transport.edge_id
        network_id = self.transport.network_id
        self.in_flight += 1
        self._sent_recent.append(time.monotonic())
        t0 = time.perf_counter()
        try:
            reply = await self.transport.request_primary(
                P.message(P.ECHO_FRAME, session_id=self.cfg.session_id,
                          frame_no=frame_no, payload_bytes=FRAME_PAYLOAD_BYTES,
                          pad="x" * 512),
                timeout=2.5)
        except Exception:
            reply = None
        finally:
            self.in_flight -= 1

        e2e_ms = (time.perf_counter() - t0) * 1000.0
        if reply is None or reply.get("type") != P.ECHO_RESULT:
            self.consecutive_failures += 1
            rec = FrameRecord(frame_no, sent_t, None, None, None, edge_id,
                              network_id, lost=True)
        else:
            self.consecutive_failures = 0
            rec = FrameRecord(frame_no, sent_t, self.now(), e2e_ms,
                              reply.get("inference_ms"), reply.get("edge_id"),
                              network_id, duplicate=bool(reply.get("duplicate")))
            # Mirror the session state locally so a handoff has something to send.
            self.state.bump(frame_no, {"tracks": reply.get("tracks", [])})
        self.metrics.add(rec)
        self.telemetry.emit("frame", **rec.as_dict())

    # -- agent loop --------------------------------------------------------

    async def _agent_loop(self) -> None:
        tick = self.cfg.agent_tick_s
        while self.running:
            await asyncio.sleep(tick)
            t = self.now()
            try:
                self.watcher.tick(t)
                self._observe_intent()
                await self._decide(t, tick)
                self._publish(t)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.telemetry.emit("agent_error", error=f"{type(exc).__name__}: {exc}")

    def _observe_intent(self) -> None:
        cutoff = time.monotonic() - 2.0
        self._sent_recent = [x for x in self._sent_recent if x >= cutoff]
        rate = len(self._sent_recent) / 2.0
        self.intent.observe(rate, FRAME_PAYLOAD_BYTES)

    async def _decide(self, t: float, tick: float) -> None:
        network_id, edge_id = self.current_pair()

        # Time spent on an edge that is no longer the best available one. This
        # is the number that separates QUIC-only from ECHO.
        best = self.decision.best()
        if best is not None and best.edge_id != edge_id:
            self.metrics.suboptimal_edge_s += tick

        if self.cfg.mode == "tcp":
            await self._decide_tcp(t)
            return
        if self.cfg.mode == "quic":
            await self._decide_quic(t)
            return
        await self._decide_echo(t)

    # --- tcp baseline -----------------------------------------------------

    async def _decide_tcp(self, t: float) -> None:
        broken = (not getattr(self.transport, "alive", False)
                  or self.consecutive_failures >= 3)
        if not broken:
            return
        start = t
        self.telemetry.emit("tcp_connection_lost",
                            edge_id=self.transport.edge_id,
                            failures=self.consecutive_failures)
        self._explain("The connection dropped when the network changed. "
                      "Reconnecting and rebuilding the inference session from "
                      "scratch, because a TCP connection cannot follow you.")
        await self.transport.close()

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and self.running:
            self.watcher.tick(self.now())
            cand = self.decision.best()
            if cand is not None:
                if await self.transport.open_primary(cand.edge_id, cand.network_id):
                    reply = await self.transport.request_primary(
                        P.message(P.ECHO_INIT, session_id=self.cfg.session_id,
                                  model_id=self.cfg.model_id,
                                  model_version=self.cfg.model_version), 4.0)
                    if reply is not None:
                        self.consecutive_failures = 0
                        self.metrics.reconnects += 1
                        # A cold session: the tracking state was not carried
                        # over, so the state version restarts at zero.
                        self.state = P.SessionState(
                            session_id=self.cfg.session_id,
                            model_id=self.cfg.model_id,
                            model_version=self.cfg.model_version)
                        self.metrics.mark_handoff_window(start, self.now())
                        self.telemetry.emit(
                            "tcp_reconnected", edge_id=cand.edge_id,
                            network_id=cand.network_id,
                            outage_ms=round((self.now() - start) * 1000, 1))
                        self._explain(
                            f"Reconnected to {EDGES[cand.edge_id].label} after "
                            f"{(self.now() - start) * 1000:.0f} ms. The session had "
                            f"to be recreated, so the first frames are slower.")
                        return
            await asyncio.sleep(self.cfg.tcp_reconnect_timeout_s / 3)

    # --- quic-only baseline ----------------------------------------------

    async def _decide_quic(self, t: float) -> None:
        """Keep the connection alive across networks, but never move compute."""
        pinned = self.pinned_edge or self.transport.edge_id or "A"
        best_net = self.watcher.best_network_for(pinned)
        current_net = self.transport.network_id
        if best_net is None or best_net == current_net:
            return
        cur_prof = self.watcher.latest_networks.get(current_net)
        cur_cost = (self.watcher.path_rtt_ms(current_net, pinned)
                    if cur_prof and cur_prof.available else float("inf"))
        new_cost = self.watcher.path_rtt_ms(best_net, pinned)
        if new_cost < cur_cost * 0.8 or cur_cost == float("inf"):
            start = self.now()
            await self.transport.migrate_primary(best_net)
            self.metrics.mark_handoff_window(start, self.now() + 0.5)
            self._explain(
                f"Network changed to {best_net}; the QUIC connection migrated "
                f"without reconnecting. Inference is still running on "
                f"{EDGES[pinned].label}, which is now "
                f"{new_cost:.0f} ms away.")

    # --- echo -------------------------------------------------------------

    async def _decide_echo(self, t: float) -> None:
        if self.handoff.state not in ("STABLE",):
            return
        network_id, edge_id = self.current_pair()
        current, challenger, reason = self.decision.evaluate(network_id, edge_id)

        self.last_reason = reason

        if challenger is None:
            diag = self.troubleshooter.diagnose(current)
            self.last_diagnosis = diag
            self.telemetry.emit("diagnosis", **diag)
            if diag["cause"] == "healthy":
                self._explain(self.explainer.why_stay(current, reason))
            else:
                self._explain(self.explainer.trouble(diag))
            return

        if self.recovery.is_blocked(challenger.edge_id):
            return

        # Same compute, different path: that is a transport-level move only,
        # and QUIC does it without any session work at all.
        if challenger.edge_id == edge_id and challenger.network_id != network_id:
            start = self.now()
            if await self.transport.migrate_primary(challenger.network_id):
                self.metrics.mark_handoff_window(start, self.now() + 0.3)
                self._explain(
                    f"Staying on {EDGES[edge_id].label} but moving the path to "
                    f"{challenger.network_id}; the connection migrated without "
                    f"a reconnect. {reason.capitalize()}.")
            return

        # Compute has to move: run the full ECHO sequence, starting early.
        self._explain(self.explainer.preparing(challenger))
        start = self.now()
        self.telemetry.emit("handoff_begin", source_edge=edge_id,
                            target_edge=challenger.edge_id,
                            target_network=challenger.network_id,
                            reason=reason,
                            predicted_gain_ms=round(current.e2e_ms - challenger.e2e_ms, 1))

        result = await self.handoff.execute(edge_id, challenger, self.state)
        self.metrics.mark_handoff_window(start, self.now())

        self.last_handoff_ms = result.total_ms
        if result.ok:
            self.metrics.handoffs.append(result.as_dict())
            self.metrics.state_bytes += result.state_bytes
            self._explain(self.explainer.committed(
                result.source_edge, result.target_edge, result.state_version,
                result.total_ms))
        else:
            self.metrics.failed_handoffs += 1
            plan = self.recovery.plan(result, current)
            self.telemetry.emit("recovery", action=plan.action, reason=plan.reason)
            self._explain(self.explainer.recovered(
                result.target_edge, current.edge_id, result.reason))
            if plan.action == RETRY_ELSEWHERE and plan.target is not None:
                await self.transport.open_primary(plan.target.edge_id,
                                                  plan.target.network_id)
                await self.transport.request_primary(
                    P.message(P.ECHO_INIT, session_id=self.cfg.session_id,
                              model_id=self.cfg.model_id,
                              model_version=self.cfg.model_version), 4.0)

    # -- dashboard feed ----------------------------------------------------

    def _agents_snapshot(self) -> Dict[str, Any]:
        """One line of live truth per agent, so the pipeline can be watched.

        Everything here is read straight off the agents themselves rather than
        recomputed for display, so the dashboard cannot show a decision that
        differs from the one that was actually taken.
        """
        network_id, edge_id = self.current_pair()
        trends: Dict[str, Any] = {}
        for nid in self.watcher.latest_networks:
            p = self.predictor.network(nid)
            ttl = self.predictor.time_to_unusable_s(nid)
            trends[nid] = {
                "rtt_slope": round(p["rtt_ms_slope"], 2),
                "quality_slope": round(p["quality_slope"], 4),
                "loss_slope": round(p["loss_slope"], 5),
                "predicted_rtt_ms": round(p["rtt_ms"], 1),
                "predicted_quality": round(p["quality"], 3),
                "degrading": self.predictor.degrading(nid),
                "seconds_to_unusable": None if ttl == float("inf") else round(ttl, 1),
            }

        ranked = [c for c in self.decision.last_ranking if c.reachable][:3]
        current = self.decision.current_candidate(network_id, edge_id)

        return {
            "watcher": {
                "networks": len(self.watcher.latest_networks),
                "edges": len(self.watcher.latest_edges),
                "tick_ms": int(self.cfg.agent_tick_s * 1000),
                "samples": sum(len(h["quality"]) for h in self.watcher.net_history.values()),
            },
            "predictor": {"horizon_s": self.cfg.prediction_horizon_s, "trends": trends},
            "intent": {
                "intent": self.intent.intent,
                "description": self.intent.describe(),
                "live": self.intent.is_live,
                "preload_buffer_s": self.intent.preload_buffer_s,
            },
            "decision": {
                "current": current.as_dict(),
                "candidates": [c.as_dict() for c in ranked],
                "reason": self.last_reason,
                "margin_pct": round(self.cfg.switch_margin * 100),
                "patience": self.cfg.switch_patience,
                "streaks": self.decision.streak_view,
            },
            "handoff": {
                "state": self.handoff.state,
                "completed": len(self.metrics.handoffs),
                "failed": self.metrics.failed_handoffs,
                "last_ms": (round(self.last_handoff_ms, 1)
                            if self.last_handoff_ms is not None else None),
                "state_bytes": self.metrics.state_bytes,
            },
            "recovery": {
                "recoveries": self.recovery.recoveries,
                "blocked": self.recovery.blocked_edges(),
            },
            "troubleshooter": self.last_diagnosis or {"cause": "healthy",
                                                      "headline": "nothing is wrong",
                                                      "detail": ""},
            "explainer": {"text": self.last_explanation},
        }

    def _publish(self, t: float) -> None:
        network_id, edge_id = self.current_pair()
        ranking = self.decision.last_ranking[:6]
        recent = [f for f in self.metrics.frames[-40:] if f.e2e_ms is not None]
        live_ms = round(sum(f.e2e_ms for f in recent) / len(recent), 1) if recent else None
        self.telemetry.emit(
            "state",
            mode=self.cfg.mode,
            world=self.world.snapshot(),
            edges=self.watcher.latest_edges,
            active_edge=edge_id,
            active_network=network_id,
            handoff_state=self.handoff.state,
            intent=self.intent.intent,
            intent_desc=self.intent.describe(),
            preload_buffer_s=self.intent.preload_buffer_s,
            live_e2e_ms=live_ms,
            ranking=[c.as_dict() for c in ranking],
            predicted_best=(self.decision.best().as_dict()
                            if self.decision.best() else None),
            explanation=self.last_explanation,
            agents=self._agents_snapshot(),
            handoffs=len(self.metrics.handoffs),
            failed_handoffs=self.metrics.failed_handoffs,
            reconnects=self.metrics.reconnects,
            frames_sent=len(self.metrics.frames),
            frames_lost=sum(1 for f in self.metrics.frames if f.lost),
        )
