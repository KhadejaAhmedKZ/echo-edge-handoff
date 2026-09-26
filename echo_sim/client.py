"""Robot client and the controller that wires the agents to the transport.

One process plays the inspection robot: it sends requests at a fixed rate (or,
for buffered playback, fetches content segments ahead), sends each one to the
currently active server, and records what came back and how long it took.
Alongside it, the agent loop runs on its own tick, watching every network and
every server and deciding whether the session should move.

Four controller modes share every line of this file except the decision
branch, which is the only honest way to compare them:

  tcp_reconnect  no migration, no prediction. React after the break, reconnect,
                 and rebuild the session from nothing.
  quic_fixed     the connection survives a network change, but the processing
                 server never moves. Separates transport continuity from
                 server selection.
  measured       ECHO migration driven by smoothed current measurements
                 (prediction horizon 0).
  predictive     ECHO migration driven by the Predictor's trend over the
                 configured horizon.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from . import events as E
from . import protocol as P
from .agents.decision import DecisionEngine
from .agents.explainer import Explainer
from .agents.handoff import PHASE_NAMES, HandoffManager
from .agents.intent import IntentAgent
from .agents.orchestrator import HANDOFF, HOLD, MIGRATE_PATH, Orchestrator
from .agents.predictor import Predictor
from .agents.recovery import RETRY_ELSEWHERE, RecoveryAgent
from .agents.troubleshooter import Troubleshooter
from .agents.watcher import Watcher
from .config import DEADLINE_THRESHOLDS_MS, EDGES, RunConfig
from .metrics import FrameRecord, RunMetrics
from .telemetry import Telemetry
from .world import World

FRAME_PAYLOAD_BYTES = 24_000      # a modest JPEG from an inspection camera
SEGMENT_PAYLOAD_BYTES = 400_000   # one second of prerecorded video
MAX_IN_FLIGHT = 48
METRICS_EVERY_TICKS = 4

CONTINUITY_PROPERTY_AFTER = (
    "first result from the new server carries the same session_id and a "
    "state_version greater than the transferred one (progress continued, "
    "not restarted)")
CONTINUITY_PROPERTY_RECONNECT = (
    "first result after reconnecting carries a state_version greater than the "
    "last one before the connection was lost")


class EchoController:
    def __init__(self, cfg: RunConfig, world: World, edge_services: Dict[str, Any],
                 transport, telemetry: Telemetry) -> None:
        self.cfg = cfg
        self.mode = E.canonical_mode(cfg.mode)
        self.world = world
        self.edges = edge_services
        self.transport = transport
        self.telemetry = telemetry
        self.playback = cfg.profile == "buffered_playback"

        horizon = 0.0 if self.mode == E.MEASURED else cfg.prediction_horizon_s
        self.horizon_s = horizon
        self.watcher = Watcher(world, edge_services)
        self.predictor = Predictor(self.watcher, horizon)
        self.intent = IntentAgent(profile=cfg.profile)
        self.decision = DecisionEngine(self.watcher, self.predictor, self.intent,
                                       lambda: world.position,
                                       cfg.switch_margin, cfg.switch_patience,
                                       stable_link_hold_s=cfg.stable_link_hold_s)
        # Mode-independent referee for "time on a server that was not the best":
        # measurement only, so no mode is judged by its own forecast.
        self.reference = DecisionEngine(self.watcher, Predictor(self.watcher, 0.0),
                                        IntentAgent(profile=cfg.profile),
                                        lambda: world.position,
                                        cfg.switch_margin, cfg.switch_patience,
                                       stable_link_hold_s=cfg.stable_link_hold_s)
        self.troubleshooter = Troubleshooter(self.watcher, self.predictor)
        self.explainer = Explainer()
        self.handoff = HandoffManager(transport, cfg, telemetry, self.explainer)
        self.handoff.last_sent_fn = lambda: self.frame_no
        self.recovery = RecoveryAgent(self.watcher, self.decision)
        self.orchestrator = Orchestrator(
            self.decision, self.handoff, self.recovery, self.intent,
            buffer_provider=(lambda: self.buffer_s) if self.playback else None,
            buffer_hold_s=cfg.buffer_hold_s)

        self.metrics = RunMetrics(mode=self.mode, deadline_ms=cfg.deadline_ms,
                                  thresholds_ms=DEADLINE_THRESHOLDS_MS,
                                  reversal_window_s=cfg.reversal_window_s,
                                  profile=cfg.profile)
        self.state = P.SessionState(session_id=cfg.session_id,
                                    model_id=cfg.model_id,
                                    model_version=cfg.model_version)
        self.frame_no = 1000
        self.in_flight = 0
        self.consecutive_failures = 0
        self.pinned_edge: Optional[str] = None      # quic_fixed stays here
        self.last_explanation = ""
        self.last_reason = ""
        self.last_server_state_version: Optional[int] = None
        self.pending_continuity: Optional[Dict[str, Any]] = None
        self.handoff_target: Optional[Dict[str, str]] = None
        self.zone: Optional[str] = None
        self.running = False
        self._ticks = 0
        self._t0 = time.monotonic()
        self._sent_recent: List[float] = []
        # buffered playback
        self.buffer_s = 0.0
        self.playing = False
        self.stalled = False
        self._seg_in_flight = 0

    # -- helpers -----------------------------------------------------------

    def now(self) -> float:
        return time.monotonic() - self._t0

    def emit(self, event_type: str, source: str, **payload) -> None:
        self.telemetry.emit(event_type, source, **payload)

    def current_pair(self) -> tuple[str, str]:
        return (self.transport.network_id or "wifi",
                self.transport.edge_id or "A")

    def _explain(self, text: str) -> None:
        if text and text != self.last_explanation:
            self.last_explanation = text
            self.emit("explanation", "explainer", text=text)

    def _decided(self, action: str, reason: str, *, veto: str = "",
                 target=None, current=None, policy: str = "") -> None:
        self.last_reason = reason
        self.emit(
            "decision_made", "orchestrator" if self.mode in (E.MEASURED, E.PREDICTIVE)
            else "client",
            action=action, reason=reason, veto=veto,
            policy=policy or self.mode,
            current_server=self.transport.edge_id,
            current_network=self.transport.network_id,
            current_expected_ms=(round(current.e2e_ms, 1)
                                 if current is not None and current.reachable else None),
            target_server=target.edge_id if target is not None else None,
            target_network=target.network_id if target is not None else None,
            target_expected_ms=round(target.e2e_ms, 1) if target is not None else None,
            detail={
                "orchestrator": self.orchestrator.snapshot(),
                "streaks": self.decision.streak_view,
                "horizon_s": self.horizon_s,
                "margin_pct": round(self.cfg.switch_margin * 100),
                "patience": self.cfg.switch_patience,
            })

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
        if self.mode == E.QUIC_FIXED:
            self.pinned_edge = best.edge_id
        self.zone = self.world.zone_label()
        self.emit("session_started", "client", server=best.edge_id,
                  network=best.network_id, session_id=self.cfg.session_id,
                  expected_ms=round(best.e2e_ms, 1))
        self._explain(
            f"Session {self.cfg.session_id} started on Server {best.edge_id} over "
            f"{best.network_id}; expected response time {best.e2e_ms:.0f} ms.")
        return True

    async def run(self) -> RunMetrics:
        self.running = True
        work = self._playback_loop() if self.playback else self._frame_loop()
        tasks = [asyncio.ensure_future(work),
                 asyncio.ensure_future(self._agent_loop())]
        try:
            await asyncio.sleep(self.cfg.duration_s)
        finally:
            self.running = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0.3)   # let the last few results land
        self._emit_metrics()
        return self.metrics

    # -- request path ------------------------------------------------------

    async def _frame_loop(self) -> None:
        interval = self.cfg.frame_interval_s
        next_at = time.monotonic()
        pending: set = set()
        try:
            while self.running:
                next_at += interval
                await asyncio.sleep(max(0.0, next_at - time.monotonic()))
                self.frame_no += 1
                if self.in_flight >= MAX_IN_FLIGHT:
                    # Backpressure: the path cannot keep up, so this request is
                    # lost rather than queued behind a queue already too long.
                    self._record_lost(self.frame_no, self.now(), "backpressure",
                                      self.transport.edge_id, self.transport.network_id)
                    continue
                task = asyncio.ensure_future(self._send(self.frame_no,
                                                        FRAME_PAYLOAD_BYTES))
                pending.add(task)
                task.add_done_callback(pending.discard)
        finally:
            for t in list(pending):
                t.cancel()

    def _record_lost(self, request_no: int, sent_t: float, reason: str,
                     edge_id, network_id) -> None:
        rec = FrameRecord(request_no, sent_t, None, None, None, edge_id,
                          network_id, lost=True)
        self.metrics.add(rec)
        self.emit("request_lost", "client", request_no=request_no,
                  sent_t=round(sent_t, 4), reason=reason, server=edge_id,
                  network=network_id)

    async def _send(self, request_no: int, payload_bytes: int) -> bool:
        """Send one request; record its outcome. Returns True on a result."""
        sent_t = self.now()
        edge_id = self.transport.edge_id
        network_id = self.transport.network_id
        self.in_flight += 1
        self._sent_recent.append(time.monotonic())
        t0 = time.perf_counter()
        try:
            reply = await self.transport.request_primary(
                P.message(P.ECHO_FRAME, session_id=self.cfg.session_id,
                          frame_no=request_no, payload_bytes=payload_bytes,
                          pad="x" * 512),
                timeout=self.cfg.request_timeout_s)
        except asyncio.CancelledError:
            self.metrics.unfinished_at_end += 1
            raise
        except Exception:
            reply = None
        finally:
            self.in_flight -= 1

        response_ms = (time.perf_counter() - t0) * 1000.0
        if reply is None or reply.get("type") != P.ECHO_RESULT:
            self.consecutive_failures += 1
            reason = ("timeout" if reply is None
                      else f"server error: {reply.get('reason', 'unknown')}")
            self._record_lost(request_no, sent_t, reason, edge_id, network_id)
            return False

        self.consecutive_failures = 0
        server = reply.get("edge_id")
        rec = FrameRecord(request_no, sent_t, self.now(), response_ms,
                          reply.get("inference_ms"), server, network_id,
                          duplicate=bool(reply.get("duplicate")))
        # Mirror the session state locally so a handoff has something to send.
        self.state.bump(request_no, {"tracks": reply.get("tracks", [])})
        self.metrics.add(rec)
        sv = reply.get("state_version")
        self.emit("request_completed", "client", request_no=request_no,
                  sent_t=round(sent_t, 4), recv_t=round(rec.recv_t, 4),
                  response_ms=round(response_ms, 2),
                  processing_ms=(round(rec.inference_ms, 2)
                                 if rec.inference_ms is not None else None),
                  server=server, network=network_id, duplicate=rec.duplicate,
                  session_id=reply.get("session_id"), state_version=sv)
        self._check_continuity(reply)
        if sv is not None and server == self.transport.edge_id:
            self.last_server_state_version = sv
        return True

    def _check_continuity(self, reply: Dict[str, Any]) -> None:
        pend = self.pending_continuity
        if pend is None or reply.get("edge_id") != pend["server"]:
            return
        self.pending_continuity = None
        sv = reply.get("state_version")
        sid_ok = reply.get("session_id") == self.cfg.session_id
        version_ok = sv is not None and sv > pend["baseline_version"]
        ok = sid_ok and version_ok
        check = {
            "ok": ok, "stage": pend["stage"], "property": pend["property"],
            "session_id": reply.get("session_id"), "server": pend["server"],
            "baseline_state_version": pend["baseline_version"],
            "reported_state_version": sv,
            "checks": {"session_id": sid_ok, "state_version_continues": version_ok},
        }
        self.metrics.continuity_checks.append(check)
        self.emit("state_verified", "client", **check)

    # -- buffered playback -------------------------------------------------

    async def _playback_loop(self) -> None:
        """Fetch content segments ahead and play them back in real time.

        This is a controlled workload, not a real video player: each request is
        one `segment_s` second of prerecorded content. The buffer drains in
        real time once playback has started; an empty buffer is a stall.
        """
        tick = 0.05
        last = time.monotonic()
        pending: set = set()
        stall_started: Optional[float] = None
        try:
            while self.running:
                await asyncio.sleep(tick)
                now = time.monotonic()
                dt, last = now - last, now
                if self.playing and not self.stalled:
                    self.buffer_s = max(0.0, self.buffer_s - dt)
                    if self.buffer_s <= 0.0:
                        self.stalled = True
                        self.metrics.stalls += 1
                        stall_started = now
                elif self.stalled and self.buffer_s >= self.cfg.segment_s:
                    self.stalled = False
                    if stall_started is not None:
                        self.metrics.stall_time_s += now - stall_started
                        stall_started = None
                elif not self.playing and self.buffer_s >= self.cfg.playback_start_s:
                    self.playing = True
                    self.metrics.playback_started = True
                if self.playing:
                    mb = self.metrics.min_buffer_s
                    self.metrics.min_buffer_s = (self.buffer_s if mb is None
                                                 else min(mb, self.buffer_s))
                ahead = self.buffer_s + self._seg_in_flight * self.cfg.segment_s
                if ahead < self.cfg.buffer_target_s and self._seg_in_flight < 2:
                    self.frame_no += 1
                    self._seg_in_flight += 1
                    task = asyncio.ensure_future(self._fetch_segment(self.frame_no))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
        finally:
            if stall_started is not None:
                self.metrics.stall_time_s += time.monotonic() - stall_started
            for t in list(pending):
                t.cancel()

    async def _fetch_segment(self, request_no: int) -> None:
        try:
            if await self._send(request_no, SEGMENT_PAYLOAD_BYTES):
                self.buffer_s += self.cfg.segment_s
        finally:
            self._seg_in_flight -= 1

    # -- agent loop --------------------------------------------------------

    async def _agent_loop(self) -> None:
        tick = self.cfg.agent_tick_s
        while self.running:
            await asyncio.sleep(tick)
            t = self.now()
            try:
                self.watcher.tick(t)
                self._observe_intent()
                self._observe_world()
                await self._decide(t, tick)
                self._publish()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.emit("agent_error", "client",
                          error=f"{type(exc).__name__}: {exc}")

    def _observe_intent(self) -> None:
        cutoff = time.monotonic() - 2.0
        self._sent_recent = [x for x in self._sent_recent if x >= cutoff]
        rate = len(self._sent_recent) / 2.0
        size = SEGMENT_PAYLOAD_BYTES if self.playback else FRAME_PAYLOAD_BYTES
        self.intent.observe(rate, size)

    def _observe_world(self) -> None:
        zone = self.world.zone_label()
        if self.zone is not None and zone != self.zone:
            self.emit("zone_changed", "world", from_zone=self.zone, to_zone=zone,
                      position=round(self.world.position, 4))
        self.zone = zone

    async def _decide(self, t: float, tick: float) -> None:
        network_id, edge_id = self.current_pair()

        ref = self.reference.best(hosting=edge_id)
        if ref is not None and ref.edge_id != edge_id:
            self.metrics.suboptimal_edge_s += tick

        if self.mode == E.TCP_RECONNECT:
            await self._decide_tcp(t)
        elif self.mode == E.QUIC_FIXED:
            await self._decide_quic(t)
        else:
            await self._decide_echo(t)

    # --- tcp baseline -----------------------------------------------------

    async def _decide_tcp(self, t: float) -> None:
        self.decision.rank(hosting=self.transport.edge_id)
        broken = (not getattr(self.transport, "alive", False)
                  or self.consecutive_failures >= 3)
        if not broken:
            self._decided(HOLD, "fixed policy: keep this connection until it "
                          "breaks", policy="reconnect after failure")
            return
        start = t
        old_server = self.transport.edge_id
        version_before = self.last_server_state_version or 0
        self.emit("connection_lost", "transport", server=old_server,
                  network=self.transport.network_id,
                  failures=self.consecutive_failures)
        self._decided("reconnect", "the connection broke; dialling again",
                      policy="reconnect after failure")
        self._explain("The connection dropped when the network changed. "
                      "Reconnecting and rebuilding the session from scratch, "
                      "because a TCP connection cannot follow the robot.")
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
                        # A cold session: progress was not carried over.
                        self.state = P.SessionState(
                            session_id=self.cfg.session_id,
                            model_id=self.cfg.model_id,
                            model_version=self.cfg.model_version)
                        self.metrics.mark_handoff_window(start, self.now())
                        self.metrics.note_server_switch(self.now(), old_server,
                                                        cand.edge_id, "reconnect")
                        self.pending_continuity = {
                            "server": cand.edge_id, "stage": "after_reconnect",
                            "baseline_version": version_before,
                            "property": CONTINUITY_PROPERTY_RECONNECT}
                        outage = round((self.now() - start) * 1000, 1)
                        self.emit("reconnected", "transport", server=cand.edge_id,
                                  network=cand.network_id, outage_ms=outage,
                                  cold_start=True)
                        self._explain(
                            f"Reconnected to Server {cand.edge_id} after {outage:.0f} "
                            f"ms. The session had to be recreated, so progress "
                            f"restarted and the first results are slower.")
                        return
            await asyncio.sleep(self.cfg.tcp_reconnect_timeout_s / 3)

    # --- quic-fixed baseline ---------------------------------------------

    async def _decide_quic(self, t: float) -> None:
        """Keep the connection alive across networks, but never move the server."""
        self.decision.rank(hosting=self.transport.edge_id)
        pinned = self.pinned_edge or self.transport.edge_id or "A"
        best_net = self.watcher.best_network_for(pinned)
        current_net = self.transport.network_id
        policy = "fixed server, best network"
        if best_net is None or best_net == current_net:
            self._decided(HOLD, f"fixed policy: Server {pinned} never moves; "
                          f"already on its best network", policy=policy)
            return
        cur_prof = self.watcher.latest_networks.get(current_net)
        cur_cost = (self.watcher.path_rtt_ms(current_net, pinned)
                    if cur_prof and cur_prof.available else float("inf"))
        new_cost = self.watcher.path_rtt_ms(best_net, pinned)
        if new_cost < cur_cost * 0.8 or cur_cost == float("inf"):
            start = self.now()
            self._decided(MIGRATE_PATH, f"{best_net} reaches Server {pinned} "
                          f"faster; moving the connection only", policy=policy)
            if await self.transport.migrate_primary(best_net):
                self.metrics.path_migrations += 1
            self.metrics.mark_handoff_window(start, self.now() + 0.5)
            self._explain(
                f"Network changed to {best_net}; the QUIC connection moved "
                f"without reconnecting. Processing is still on Server {pinned}, "
                f"now about {new_cost:.0f} ms away.")
        else:
            self._decided(HOLD, f"fixed policy: Server {pinned} never moves; "
                          f"{best_net} is not enough faster to move the "
                          f"connection", policy=policy)

    # --- measured / predictive -------------------------------------------

    async def _decide_echo(self, t: float) -> None:
        network_id, edge_id = self.current_pair()
        action, current = self.orchestrator.plan(network_id, edge_id)

        if action.kind == HOLD:
            self._decided(HOLD, action.reason, veto=action.veto, current=current)
            diag = self.troubleshooter.diagnose(current)
            if self._ticks % 4 == 0:
                self.emit("diagnosis", "troubleshooter", **diag)
            if action.veto:
                self._explain(f"Holding. {action.reason[:1].upper()}{action.reason[1:]}.")
            elif diag["cause"] == "healthy":
                self._explain(self.explainer.why_stay(current, action.reason))
            else:
                self._explain(self.explainer.trouble(diag))
            return

        challenger = action.target
        self._decided(action.kind, action.reason, target=challenger, current=current)

        # Same server, different network: a transport-level move only.
        if action.kind == MIGRATE_PATH:
            start = self.now()
            if await self.transport.migrate_primary(challenger.network_id):
                self.orchestrator.note_move()
                self.metrics.path_migrations += 1
                self.metrics.mark_handoff_window(start, self.now() + 0.3)
                self._explain(
                    f"Staying on Server {edge_id} but moving the connection to "
                    f"{challenger.network_id}; no reconnect was needed.")
            return

        # The server has to change: run the full ECHO sequence, starting early.
        self._explain(self.explainer.preparing(current, challenger))
        start = self.now()
        self.handoff_target = {"server": challenger.edge_id,
                               "network": challenger.network_id}
        self.emit("handoff_phase_changed", "orchestrator", phase="selected",
                  technical_phase="SELECTED", source_server=edge_id,
                  target_server=challenger.edge_id,
                  target_network=challenger.network_id, reason=action.reason,
                  expected_gain_ms=round(current.e2e_ms - challenger.e2e_ms, 1))

        result = await self.handoff.execute(edge_id, challenger, self.state)
        self.metrics.mark_handoff_window(start, self.now())
        if self.handoff.last_ready_check is not None:
            self.metrics.continuity_checks.append(self.handoff.last_ready_check)
        self.handoff_target = None
        self.orchestrator.note_move()

        if result.ok:
            self.metrics.handoffs.append(result.as_dict())
            self.metrics.state_bytes += result.state_bytes
            self.metrics.note_server_switch(self.now(), edge_id, challenger.edge_id,
                                            "handoff")
            if current.reachable:
                # Only a discretionary move arms the anti ping-pong guard; a
                # server left because its path died may be returned to freely.
                self.decision.note_left(edge_id)
            self.pending_continuity = {
                "server": challenger.edge_id, "stage": "first_result_after_switch",
                "baseline_version": result.state_version,
                "property": CONTINUITY_PROPERTY_AFTER}
            self._explain(self.explainer.committed(
                result.source_edge, result.target_edge, result.state_version,
                result.total_ms))
        else:
            self.metrics.failed_handoffs += 1
            plan = self.recovery.plan(result, current)
            self.emit("recovery_planned", "recovery", action=plan.action,
                      reason=plan.reason,
                      target_server=plan.target.edge_id if plan.target else None)
            self._explain(self.explainer.recovered(
                result.target_edge, current.edge_id, result.reason))
            if plan.action == RETRY_ELSEWHERE and plan.target is not None:
                if await self.transport.open_primary(plan.target.edge_id,
                                                     plan.target.network_id):
                    await self.transport.request_primary(
                        P.message(P.ECHO_INIT, session_id=self.cfg.session_id,
                                  model_id=self.cfg.model_id,
                                  model_version=self.cfg.model_version), 4.0)
                    self.metrics.note_server_switch(self.now(), edge_id,
                                                    plan.target.edge_id,
                                                    "cold_restart")

    # -- dashboard feed ----------------------------------------------------

    def _publish(self) -> None:
        self._ticks += 1
        network_id, edge_id = self.current_pair()
        snap = self.world.snapshot()
        self.emit(
            "network_observed", "watcher",
            position=snap["position"], mission_fraction=snap["mission_fraction"],
            zone=snap["zone"], networks=snap["networks"],
            disturbances=snap["disturbances"],
            servers=self.watcher.latest_edges,
            current_server=edge_id, current_network=network_id,
            handoff_phase=PHASE_NAMES.get(self.handoff.state, "idle"),
            candidate=self.handoff_target,
            server_state_version=self.last_server_state_version,
            last_completed_request=self.state.last_processed_frame,
            intent=self.intent.intent, observed_traffic=self.intent.observed)

        ranking = [c for c in self.decision.last_ranking if c.reachable][:6]
        current = self.decision.current_candidate(network_id, edge_id)
        self.emit("candidate_scored", "decision",
                  candidates=[c.as_dict() for c in ranking],
                  current=current.as_dict(), horizon_s=self.horizon_s)

        if self.playback:
            self.emit("playback_buffer", "client", buffer_s=round(self.buffer_s, 2),
                      stalls=self.metrics.stalls, playing=self.playing,
                      stalled=self.stalled,
                      stall_time_s=round(self.metrics.stall_time_s, 2))

        if self._ticks % METRICS_EVERY_TICKS == 0:
            self._emit_metrics()

    def _emit_metrics(self) -> None:
        self.emit("metrics_updated", "metrics", summary=self.metrics.summary())
