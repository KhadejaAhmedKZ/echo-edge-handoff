"""QUIC transport for the robot client.

This is where QUIC earns its place. Three properties matter here:

* Connection migration. The client binds a different local port per access
  network. Changing network means rebinding the socket and continuing to send
  on the *same* connection ID - the server validates the new path and carries
  on. No new handshake, no new session, nothing above the transport notices.
* One handshake. QUIC brings the connection up in a single round trip, so a
  standby connection to a candidate edge is cheap enough to open speculatively.
* Independent streams. Each frame is its own stream, so a lost packet delays
  only the frame it belonged to instead of stalling every frame behind it.

The last one is the difference from TCP that shows up on a lossy link even when
nothing is switching.
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
from typing import Any, Dict, Optional

from aioquic.asyncio import QuicConnectionProtocol
from aioquic.asyncio.client import connect
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ConnectionTerminated, QuicEvent, StreamDataReceived

from .. import protocol as P
from ..config import CLIENT_PORT_BASE, CLIENT_PORT_SPAN, EDGES

ALPN = ["echo/1"]
LOOPBACK = "127.0.0.1"


def _dual_stack_socket(port: int) -> socket.socket:
    """A dual-stack UDP socket bound to `port`, matching how aioquic dials out.

    aioquic binds AF_INET6 with V6ONLY off and addresses the server as an
    IPv4-mapped address, so any socket we substitute during migration has to be
    built the same way or the mapped address will not fit it.
    """
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(("::", port, 0, 0))
    except OSError:
        sock.close()
        raise
    return sock


def pick_local_port(network_id: str) -> int:
    """A free UDP port inside this network's range, so the relay can identify it."""
    base = CLIENT_PORT_BASE[network_id]
    for offset in range(CLIENT_PORT_SPAN):
        port = base + offset
        try:
            _dual_stack_socket(port).close()
        except OSError:
            continue
        return port
    raise RuntimeError(f"no free client port for {network_id}")


def open_local_socket(network_id: str) -> tuple[socket.socket, int]:
    base = CLIENT_PORT_BASE[network_id]
    for offset in range(CLIENT_PORT_SPAN):
        port = base + offset
        try:
            return _dual_stack_socket(port), port
        except OSError:
            continue
    raise RuntimeError(f"no free client port for {network_id}")


class EchoClientProtocol(QuicConnectionProtocol):
    """Request/response over QUIC: one bidirectional stream per message."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._waiters: Dict[int, asyncio.Future] = {}
        self._decoders: Dict[int, P.FrameDecoder] = {}
        self._migrating = False
        self.terminated = False

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, ConnectionTerminated):
            self.terminated = True
            for fut in self._waiters.values():
                if not fut.done():
                    fut.set_exception(ConnectionResetError("quic terminated"))
            self._waiters.clear()
            return
        if not isinstance(event, StreamDataReceived):
            return
        dec = self._decoders.setdefault(event.stream_id, P.FrameDecoder())
        for msg in dec.feed(event.data):
            fut = self._waiters.pop(event.stream_id, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            self._decoders.pop(event.stream_id, None)

    def connection_lost(self, exc) -> None:
        # During migration the old socket is closed deliberately; the QUIC
        # connection itself is very much alive and must not be torn down.
        if self._migrating:
            return
        super().connection_lost(exc)

    async def request(self, msg: Dict[str, Any], timeout: float = 3.0
                      ) -> Optional[Dict[str, Any]]:
        if self.terminated:
            return None
        loop = asyncio.get_running_loop()
        stream_id = self._quic.get_next_available_stream_id()
        fut: asyncio.Future = loop.create_future()
        self._waiters[stream_id] = fut
        try:
            self._quic.send_stream_data(stream_id, P.encode(msg), end_stream=True)
            self.transmit()
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, ConnectionResetError, Exception):
            self._waiters.pop(stream_id, None)
            return None

    async def migrate_to(self, network_id: str) -> int:
        """Rebind to a port on another access network, same QUIC connection.

        This is the actual QUIC migration: a brand new UDP socket on a port
        belonging to the new network, handed to the *existing* connection. No
        handshake, no new connection ID negotiation from scratch, no session
        rebuild - the server simply sees the same connection arriving from a
        new four-tuple and validates the path.
        """
        loop = asyncio.get_running_loop()
        old_transport = self._transport
        sock, port = open_local_socket(network_id)
        self._migrating = True
        try:
            await loop.create_datagram_endpoint(lambda: self, sock=sock)
            if old_transport is not None:
                old_transport.close()
        finally:
            self._migrating = False
        # New connection ID on the new path, then poke the connection so the
        # server sees traffic from the new four-tuple and validates it.
        with contextlib.suppress(Exception):
            self._quic.change_connection_id()
        self.transmit()
        return port


class EchoConnection:
    """One live QUIC connection to one edge, over one access network."""

    def __init__(self, edge_id: str, network_id: str,
                 protocol: EchoClientProtocol, stack) -> None:
        self.edge_id = edge_id
        self.network_id = network_id
        self.protocol = protocol
        self._stack = stack
        self.opened = True

    async def request(self, msg, timeout: float = 3.0):
        return await self.protocol.request(msg, timeout)

    async def migrate(self, network_id: str) -> None:
        await self.protocol.migrate_to(network_id)
        self.network_id = network_id

    async def close(self) -> None:
        if not self.opened:
            return
        self.opened = False
        with contextlib.suppress(Exception):
            await self._stack.aclose()


async def open_quic(edge_id: str, network_id: str,
                    timeout: float = 6.0) -> Optional[EchoConnection]:
    spec = EDGES[edge_id]
    config = QuicConfiguration(is_client=True, alpn_protocols=ALPN,
                               idle_timeout=30.0)
    config.verify_mode = ssl.CERT_NONE
    stack = contextlib.AsyncExitStack()
    port = pick_local_port(network_id)
    try:
        proto = await asyncio.wait_for(
            stack.enter_async_context(
                connect(LOOPBACK, spec.relay_port, configuration=config,
                        create_protocol=EchoClientProtocol, local_port=port,
                        wait_connected=True)),
            timeout)
    except Exception:
        with contextlib.suppress(Exception):
            await stack.aclose()
        return None
    return EchoConnection(edge_id, network_id, proto, stack)


class QuicTransport:
    """Primary + standby connection management, as the HandoffManager expects."""

    def __init__(self, telemetry=None) -> None:
        self.primary: Optional[EchoConnection] = None
        self.standby: Optional[EchoConnection] = None
        self.telemetry = telemetry
        self.migrations = 0

    @property
    def edge_id(self) -> Optional[str]:
        return self.primary.edge_id if self.primary else None

    @property
    def network_id(self) -> Optional[str]:
        return self.primary.network_id if self.primary else None

    async def open_primary(self, edge_id: str, network_id: str) -> bool:
        conn = await open_quic(edge_id, network_id)
        if conn is None:
            return False
        if self.primary is not None:
            await self.primary.close()
        self.primary = conn
        return True

    async def migrate_primary(self, network_id: str) -> bool:
        if self.primary is None or self.primary.network_id == network_id:
            return False
        old = self.primary.network_id
        await self.primary.migrate(network_id)
        self.migrations += 1
        if self.telemetry:
            self.telemetry.emit("quic_migration", from_network=old,
                                to_network=network_id, edge_id=self.primary.edge_id)
        return True

    async def open_standby(self, edge_id: str, network_id: str) -> bool:
        await self.close_standby()
        self.standby = await open_quic(edge_id, network_id)
        return self.standby is not None

    async def request_primary(self, msg, timeout: float = 3.0):
        if self.primary is None:
            return None
        return await self.primary.request(msg, timeout)

    async def request_standby(self, msg, timeout: float = 3.0):
        if self.standby is None:
            return None
        return await self.standby.request(msg, timeout)

    async def promote_standby(self) -> None:
        if self.standby is None:
            return
        old = self.primary
        self.primary = self.standby
        self.standby = None
        if old is not None:
            asyncio.ensure_future(_close_later(old))

    async def close_standby(self) -> None:
        if self.standby is not None:
            await self.standby.close()
            self.standby = None

    async def drain(self, edge_id: str, session_id: str) -> None:
        """Best effort: the old path may already be gone, and that is fine."""
        return None

    async def close(self) -> None:
        await self.close_standby()
        if self.primary is not None:
            await self.primary.close()
            self.primary = None


async def _close_later(conn: EchoConnection, delay: float = 0.6) -> None:
    """Let the old edge finish outstanding work before the socket disappears."""
    await asyncio.sleep(delay)
    with contextlib.suppress(Exception):
        await conn.close()
