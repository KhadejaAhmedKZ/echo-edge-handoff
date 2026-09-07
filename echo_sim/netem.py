"""Software impairment relays - the portable stand-in for tc/netem.

Every packet between the client and an edge passes through one of these. The
relay looks up which access network the client is currently sending from (by
its source port), asks the World what that network looks like right now, and
then delays, jitters, drops and rate-limits the packet accordingly.

Two consequences worth knowing:

* The traffic is real. Real UDP datagrams carrying real QUIC, real TCP streams.
  Only the *impairment* is emulated, which is exactly what netem does.
* Because the client binds a different local port per access network, moving
  between networks is a genuine QUIC path change: same connection ID, new
  four-tuple, server-side path validation. Nothing is faked at the protocol level.
"""
from __future__ import annotations

import asyncio
import random
import socket
from typing import Dict, Optional, Tuple

from .config import CLIENT_PORT_BASE, CLIENT_PORT_SPAN, EDGES, backhaul_ms
from .world import World

PORT_TO_NETWORK: Dict[int, str] = {
    base + offset: nid
    for nid, base in CLIENT_PORT_BASE.items()
    for offset in range(CLIENT_PORT_SPAN)
}
LOOPBACK = "127.0.0.1"


class _Pacer:
    """Serialisation + queueing delay for one direction of one flow."""

    def __init__(self) -> None:
        self.next_free = 0.0

    def schedule(self, loop: asyncio.AbstractEventLoop, nbytes: int,
                 bandwidth_mbps: float) -> float:
        """Return the extra queueing delay (seconds) this packet must wait."""
        if bandwidth_mbps <= 0:
            return 0.0
        now = loop.time()
        serialise = (nbytes * 8.0) / (bandwidth_mbps * 1e6)
        start = max(now, self.next_free)
        self.next_free = start + serialise
        return (start - now) + serialise


class Impairment:
    """Shared delay/loss/jitter maths used by both relays."""

    def __init__(self, world: World, edge_id: str, rng: random.Random) -> None:
        self.world = world
        self.edge_id = edge_id
        self.rng = rng
        self.pacers: Dict[Tuple[str, str], _Pacer] = {}
        self.dropped = 0
        self.forwarded = 0

    def network_for(self, client_addr) -> Optional[str]:
        return PORT_TO_NETWORK.get(client_addr[1])

    def delay_for(self, network_id: str, nbytes: int, direction: str,
                  loop: asyncio.AbstractEventLoop) -> Optional[float]:
        """Seconds to hold this packet, or None if it should be dropped."""
        profile = self.world.profile(network_id)
        if not profile.available:
            self.dropped += 1
            return None

        one_way_loss = 1.0 - (1.0 - min(profile.loss, 0.9)) ** 0.5
        if self.rng.random() < one_way_loss:
            self.dropped += 1
            return None

        # Half the access RTT, plus the backhaul hop if this edge is not the
        # home edge of the network we are currently attached to.
        base = profile.rtt_ms / 2.0 + backhaul_ms(network_id, self.edge_id)
        jitter = self.rng.gauss(0.0, profile.jitter_ms / 2.0)
        delay_ms = max(0.1, base + jitter)

        pacer = self.pacers.setdefault((network_id, direction), _Pacer())
        queueing = pacer.schedule(loop, nbytes, profile.bandwidth_mbps)
        self.forwarded += 1
        return delay_ms / 1000.0 + queueing


# --------------------------------------------------------------------------
# UDP relay (carries QUIC)
# --------------------------------------------------------------------------


class _Upstream(asyncio.DatagramProtocol):
    """One socket per client four-tuple, so replies route back unambiguously."""

    def __init__(self, relay: "UdpRelay", client_addr) -> None:
        self.relay = relay
        self.client_addr = client_addr
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.relay.deliver_downstream(self.client_addr, data)

    def error_received(self, exc) -> None:  # pragma: no cover - loopback only
        pass


class UdpRelay(asyncio.DatagramProtocol):
    """Client-facing UDP endpoint for one edge."""

    def __init__(self, world: World, edge_id: str, seed: int = 0) -> None:
        self.world = world
        self.edge_id = edge_id
        self.edge_addr = (LOOPBACK, EDGES[edge_id].quic_port)
        self.imp = Impairment(world, edge_id, random.Random(seed + hash(edge_id) % 1000))
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.upstreams: Dict[tuple, _Upstream] = {}
        self.loop = asyncio.get_event_loop()

    def connection_made(self, transport) -> None:
        self.transport = transport

    # client -> edge
    def datagram_received(self, data: bytes, addr) -> None:
        network_id = self.imp.network_for(addr)
        if network_id is None:
            return
        delay = self.imp.delay_for(network_id, len(data), "up", self.loop)
        if delay is None:
            return
        self.loop.call_later(delay, self._send_upstream, addr, data)

    def _send_upstream(self, client_addr, data: bytes) -> None:
        up = self.upstreams.get(client_addr)
        if up is None:
            up = _Upstream(self, client_addr)
            self.upstreams[client_addr] = up
            task = self.loop.create_task(
                self.loop.create_datagram_endpoint(lambda: up, remote_addr=self.edge_addr)
            )
            task.add_done_callback(lambda _t, u=up, d=data: self._flush(u, d))
            return
        if up.transport is not None:
            up.transport.sendto(data)

    @staticmethod
    def _flush(up: _Upstream, data: bytes) -> None:
        if up.transport is not None:
            up.transport.sendto(data)

    # edge -> client
    def deliver_downstream(self, client_addr, data: bytes) -> None:
        network_id = self.imp.network_for(client_addr)
        if network_id is None:
            return
        delay = self.imp.delay_for(network_id, len(data), "down", self.loop)
        if delay is None:
            return
        self.loop.call_later(delay, self._send_downstream, client_addr, data)

    def _send_downstream(self, client_addr, data: bytes) -> None:
        if self.transport is not None:
            try:
                self.transport.sendto(data, client_addr)
            except OSError:
                pass

    def close(self) -> None:
        for up in self.upstreams.values():
            if up.transport is not None:
                up.transport.close()
        self.upstreams.clear()
        if self.transport is not None:
            self.transport.close()


async def start_udp_relays(world: World, seed: int = 0) -> Dict[str, UdpRelay]:
    loop = asyncio.get_running_loop()
    relays: Dict[str, UdpRelay] = {}
    for edge_id, spec in EDGES.items():
        relay = UdpRelay(world, edge_id, seed)
        await loop.create_datagram_endpoint(
            lambda r=relay: r,
            local_addr=(LOOPBACK, spec.relay_port),
        )
        relays[edge_id] = relay
    return relays


# --------------------------------------------------------------------------
# TCP relay (carries the TCP baseline)
# --------------------------------------------------------------------------


class TcpRelay:
    """Byte-pipe with the same impairment model.

    Loss is folded into delay rather than dropping bytes: dropping bytes from a
    TCP stream would corrupt it, whereas real TCP turns loss into retransmission
    delay anyway. Total loss of coverage closes the connection, which is exactly
    the cold-reconnect the TCP baseline is meant to expose.
    """

    def __init__(self, world: World, edge_id: str, seed: int = 0) -> None:
        self.world = world
        self.edge_id = edge_id
        self.imp = Impairment(world, edge_id, random.Random(seed + 991))
        self.server: Optional[asyncio.AbstractServer] = None
        self.resets = 0

    async def start(self) -> None:
        spec = EDGES[self.edge_id]
        self.server = await asyncio.start_server(
            self._on_client, LOOPBACK, spec.relay_port)

    async def _on_client(self, creader: asyncio.StreamReader,
                         cwriter: asyncio.StreamWriter) -> None:
        peer = cwriter.get_extra_info("peername")
        network_id = self.imp.network_for(peer)
        if network_id is None:
            cwriter.close()
            return
        try:
            ereader, ewriter = await asyncio.open_connection(
                LOOPBACK, EDGES[self.edge_id].tcp_port)
        except OSError:
            cwriter.close()
            return

        async def pump(reader, writer, direction) -> None:
            loop = asyncio.get_running_loop()
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    profile = self.world.profile(network_id)
                    if not profile.available:
                        self.resets += 1
                        raise ConnectionResetError("access network gone")
                    base = profile.rtt_ms / 2.0 + backhaul_ms(network_id, self.edge_id)
                    jitter = abs(self.imp.rng.gauss(0.0, profile.jitter_ms / 2.0))
                    # retransmission stand-in for loss
                    retrans = profile.loss * 3.0 * profile.rtt_ms
                    pacer = self.imp.pacers.setdefault(
                        (network_id, direction), _Pacer())
                    queueing = pacer.schedule(loop, len(data), profile.bandwidth_mbps)
                    await asyncio.sleep((base + jitter + retrans) / 1000.0 + queueing)
                    writer.write(data)
                    await writer.drain()
            except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                pass
            finally:
                writer.close()

        await asyncio.gather(
            pump(creader, ewriter, "up"),
            pump(ereader, cwriter, "down"),
            return_exceptions=True,
        )

    async def watchdog(self) -> None:
        """Close everything the moment the access network disappears."""
        while True:
            await asyncio.sleep(0.1)

    def close(self) -> None:
        if self.server is not None:
            self.server.close()


async def start_tcp_relays(world: World, seed: int = 0) -> Dict[str, TcpRelay]:
    relays: Dict[str, TcpRelay] = {}
    for edge_id in EDGES:
        relay = TcpRelay(world, edge_id, seed)
        await relay.start()
        relays[edge_id] = relay
    return relays


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((LOOPBACK, 0))
        return s.getsockname()[1]
