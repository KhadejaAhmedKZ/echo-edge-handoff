"""Edge inference service.

Four of these run at once, one per site. Each speaks the full ECHO message set
over QUIC and over plain TCP, so the same server code backs all three
experiments and no baseline gets an unfair implementation.

The inference itself is simulated as a controlled delay that responds to load
and to whether the session was warm-started from transferred state or rebuilt
cold. That is the honest thing to model here: what the project measures is
end-to-end inference latency and session continuity across a handoff, not the
accuracy of any particular detector. Swapping in a real YOLO forward pass means
replacing `_infer` and nothing else.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import QuicEvent, StreamDataReceived

from . import protocol as P
from .config import EDGES, RunConfig, EdgeSpec
from .tls import ensure_cert

ALPN = ["echo/1"]

ACTIVE = "active"
PREPARED = "prepared"
DRAINING = "draining"
CLOSED = "closed"


@dataclass
class EdgeSession:
    state: P.SessionState
    status: str = PREPARED
    warmup_left: int = 0
    frames_served: int = 0
    opened_ts: float = field(default_factory=time.time)


class EdgeService:
    """Transport-independent brain of one edge site."""

    def __init__(self, spec: EdgeSpec, cfg: RunConfig, telemetry=None) -> None:
        self.spec = spec
        self.cfg = cfg
        self.telemetry = telemetry
        self.sessions: Dict[str, EdgeSession] = {}
        self.rng = random.Random(cfg.seed + ord(spec.id))
        self._cpu_noise = 0.0
        self._noise_ts = time.monotonic()
        self.stats = {"frames": 0, "prepares": 0, "commits": 0, "state_bytes": 0}
        # Set >0 to make this edge refuse handoffs, for the recovery demo.
        self.fail_prepare = False

    # -- health ------------------------------------------------------------

    @property
    def active_sessions(self) -> int:
        return sum(1 for s in self.sessions.values() if s.status == ACTIVE)

    def _cpu(self) -> float:
        now = time.monotonic()
        dt = now - self._noise_ts
        if dt > 0.1:
            self._noise_ts = now
            self._cpu_noise = self._cpu_noise * math.exp(-dt / 2.0) + self.rng.gauss(0, 0.05)
        base = 0.18 + 0.22 * self.active_sessions
        return min(0.99, max(0.02, base + self._cpu_noise))

    def expected_inference_ms(self) -> float:
        """What one frame should cost right now, before any network time."""
        load = self._cpu()
        return self.spec.base_inference_ms + self.spec.load_penalty_ms * (
            self.active_sessions) + 30.0 * max(0.0, load - 0.7)

    def health(self) -> Dict[str, Any]:
        return {
            "edge_id": self.spec.id,
            "label": self.spec.label,
            "home_network": self.spec.home_network,
            "cpu": round(self._cpu(), 3),
            "active_sessions": self.active_sessions,
            "prepared_sessions": sum(1 for s in self.sessions.values()
                                     if s.status == PREPARED),
            "expected_inference_ms": round(self.expected_inference_ms(), 2),
            "available": True,
            "frames_served": self.stats["frames"],
        }

    # -- inference ---------------------------------------------------------

    async def _infer(self, session: EdgeSession, frame_no: int) -> Dict[str, Any]:
        cost = self.expected_inference_ms() + abs(self.rng.gauss(0, 1.5))
        if session.warmup_left > 0:
            # Cold session: the tracker has no history, so early frames cost more.
            cost += self.cfg.warmup_penalty_ms * (
                session.warmup_left / max(1, self.cfg.warmup_frames))
            session.warmup_left -= 1
        await asyncio.sleep(cost / 1000.0)

        # A stand-in for object tracking state that must survive a handoff.
        tracked = session.state.tracking_state.get("tracks") or []
        if not tracked:
            tracked = [{"id": 1, "cls": "valve", "x": 0.5, "y": 0.5}]
        tracked = [{**t,
                    "x": round((t["x"] + 0.004) % 1.0, 4),
                    "y": round((t["y"] + 0.002) % 1.0, 4)} for t in tracked]
        return {"tracks": tracked, "inference_ms": cost}

    # -- message handling --------------------------------------------------

    async def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        t = msg.get("type")
        sid = msg.get("session_id", "")

        if t == P.ECHO_INIT:
            st = P.SessionState(
                session_id=sid,
                model_id=msg.get("model_id", self.cfg.model_id),
                model_version=msg.get("model_version", self.cfg.model_version),
                config=msg.get("config", {}),
            )
            self.sessions[sid] = EdgeSession(
                state=st, status=ACTIVE, warmup_left=self.cfg.warmup_frames)
            self._emit("session_init", sid, cold=True)
            return P.message(P.ECHO_ACK, session_id=sid, edge_id=self.spec.id,
                             cold_start=True, state_version=st.state_version)

        if t == P.ECHO_PREPARE:
            self.stats["prepares"] += 1
            if self.fail_prepare:
                return P.message(P.ECHO_ERROR, session_id=sid,
                                 edge_id=self.spec.id, reason="prepare_refused")
            st = P.SessionState(
                session_id=sid,
                model_id=msg.get("model_id", self.cfg.model_id),
                model_version=msg.get("model_version", self.cfg.model_version),
            )
            self.sessions[sid] = EdgeSession(state=st, status=PREPARED)
            # Model is already resident; this is resource allocation only.
            await asyncio.sleep(0.004)
            self._emit("session_prepare", sid)
            return P.message(P.ECHO_ACK, session_id=sid, edge_id=self.spec.id,
                             prepared=True)

        if t == P.ECHO_STATE:
            payload = msg.get("state", {})
            self.stats["state_bytes"] += len(str(payload))
            st = P.SessionState.from_dict(payload)
            sess = self.sessions.get(sid)
            if sess is None:
                sess = EdgeSession(state=st, status=PREPARED)
                self.sessions[sid] = sess
            sess.state = st
            # State arrived, so this session is warm: no cold-start penalty.
            sess.warmup_left = 0
            await asyncio.sleep(0.003)
            self._emit("session_state", sid, state_version=st.state_version)
            return P.message(P.ECHO_READY, session_id=sid, edge_id=self.spec.id,
                             state_version=st.state_version,
                             last_processed_frame=st.last_processed_frame)

        if t == P.ECHO_VERIFY:
            sess = self.sessions.get(sid)
            if sess is None:
                return P.message(P.ECHO_ERROR, session_id=sid,
                                 edge_id=self.spec.id, reason="no_session")
            result = await self._infer(sess, msg.get("frame_no", 0))
            return P.message(P.ECHO_RESULT, session_id=sid, edge_id=self.spec.id,
                             frame_no=msg.get("frame_no", 0), verify=True,
                             inference_ms=result["inference_ms"])

        if t == P.ECHO_COMMIT:
            sess = self.sessions.get(sid)
            if sess is None:
                return P.message(P.ECHO_ERROR, session_id=sid,
                                 edge_id=self.spec.id, reason="no_session")
            sess.status = ACTIVE
            self.stats["commits"] += 1
            self._emit("session_commit", sid)
            return P.message(P.ECHO_ACK, session_id=sid, edge_id=self.spec.id,
                             committed=True,
                             last_processed_frame=sess.state.last_processed_frame)

        if t == P.ECHO_DRAIN:
            sess = self.sessions.get(sid)
            if sess is not None:
                sess.status = DRAINING
                self._emit("session_drain", sid)
            return P.message(P.ECHO_ACK, session_id=sid, edge_id=self.spec.id,
                             drained=True)

        if t == P.ECHO_FRAME:
            sess = self.sessions.get(sid)
            if sess is None or sess.status != ACTIVE:
                return P.message(P.ECHO_ERROR, session_id=sid,
                                 edge_id=self.spec.id, reason="not_active",
                                 frame_no=msg.get("frame_no", 0))
            frame_no = int(msg.get("frame_no", 0))
            duplicate = frame_no <= sess.state.last_processed_frame
            result = await self._infer(sess, frame_no)
            if not duplicate:
                sess.state.bump(frame_no, {"tracks": result["tracks"]})
                sess.frames_served += 1
                self.stats["frames"] += 1
            return P.message(
                P.ECHO_RESULT, session_id=sid, edge_id=self.spec.id,
                frame_no=frame_no, duplicate=duplicate,
                inference_ms=round(result["inference_ms"], 3),
                state_version=sess.state.state_version,
                cpu=round(self._cpu(), 3),
                active_sessions=self.active_sessions,
                tracks=result["tracks"],
            )

        if t == P.ECHO_METRICS:
            return P.message(P.ECHO_METRICS, edge_id=self.spec.id, **self.health())

        return P.message(P.ECHO_ERROR, reason=f"unknown_type:{t}")

    def get_state(self, sid: str) -> Optional[P.SessionState]:
        sess = self.sessions.get(sid)
        return sess.state if sess else None

    def _emit(self, event: str, sid: str, **extra) -> None:
        if self.telemetry is not None:
            self.telemetry.emit("edge", event=event, edge_id=self.spec.id,
                                session_id=sid, **extra)


# --------------------------------------------------------------------------
# Transport adapters
# --------------------------------------------------------------------------


class EdgeQuicProtocol(QuicConnectionProtocol):
    """One QUIC connection into an edge; each request is its own stream."""

    def __init__(self, *args, service: EdgeService, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.service = service
        self._decoders: Dict[int, P.FrameDecoder] = {}

    def quic_event_received(self, event: QuicEvent) -> None:
        if not isinstance(event, StreamDataReceived):
            return
        dec = self._decoders.setdefault(event.stream_id, P.FrameDecoder())
        for msg in dec.feed(event.data):
            asyncio.ensure_future(self._respond(event.stream_id, msg))

    async def _respond(self, stream_id: int, msg: Dict[str, Any]) -> None:
        reply = await self.service.handle(msg)
        if reply is None:
            return
        reply["req_id"] = msg.get("req_id")
        try:
            self._quic.send_stream_data(stream_id, P.encode(reply), end_stream=True)
            self.transmit()
        except Exception:
            pass


async def start_edge_quic(service: EdgeService) -> Any:
    certfile, keyfile = ensure_cert()
    config = QuicConfiguration(is_client=False, alpn_protocols=ALPN,
                               max_datagram_frame_size=65536, idle_timeout=30.0)
    config.load_cert_chain(certfile, keyfile)
    return await serve(
        "127.0.0.1", service.spec.quic_port,
        configuration=config,
        create_protocol=lambda *a, **k: EdgeQuicProtocol(*a, service=service, **k),
    )


async def start_edge_tcp(service: EdgeService) -> asyncio.AbstractServer:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        lock = asyncio.Lock()

        async def serve_one(msg):
            reply = await service.handle(msg)
            if reply is None:
                return
            reply["req_id"] = msg.get("req_id")
            async with lock:
                try:
                    writer.write(P.encode(reply))
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    pass

        pending: set = set()
        try:
            while True:
                msg = await P.read_message(reader)
                if msg is None:
                    break
                # One task per request: the edge must be able to have several
                # frames in flight, exactly as it can over QUIC. Serialising
                # here would hand the TCP baseline an artificial handicap.
                task = asyncio.ensure_future(serve_one(msg))
                pending.add(task)
                task.add_done_callback(pending.discard)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            for t in list(pending):
                t.cancel()
            writer.close()

    return await asyncio.start_server(handler, "127.0.0.1", service.spec.tcp_port)


async def start_all_edges(cfg: RunConfig, telemetry=None):
    """Bring up all four sites. Returns (services, servers)."""
    services: Dict[str, EdgeService] = {}
    servers = []
    for edge_id, spec in EDGES.items():
        svc = EdgeService(spec, cfg, telemetry)
        services[edge_id] = svc
        servers.append(await start_edge_quic(svc))
        servers.append(await start_edge_tcp(svc))
    return services, servers
