"""TCP transport - the cold-reconnect baseline.

Deliberately unhelpful, because that is the point of a baseline. There is no
migration: a TCP connection is bound to its four-tuple, so when the access
network goes away the connection goes with it. The client then has to notice
(a timeout, not an instant signal), dial again, and ask the new edge to build
the inference session from scratch - which costs a cold start on top of the
reconnect.

Requests are still pipelined and the edge still serves them concurrently, so
the comparison is against a competently written TCP client, not a straw man.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
from typing import Any, Dict, Optional

from .. import protocol as P
from ..config import EDGES
from .quic import pick_local_port

LOOPBACK = "127.0.0.1"


class TcpConnection:
    def __init__(self, edge_id: str, network_id: str,
                 reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.edge_id = edge_id
        self.network_id = network_id
        self.reader = reader
        self.writer = writer
        self.alive = True
        self._waiters: Dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._pump = asyncio.ensure_future(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            while True:
                msg = await P.read_message(self.reader)
                if msg is None:
                    break
                fut = self._waiters.pop(msg.get("req_id", -1), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except (ConnectionResetError, asyncio.CancelledError, OSError):
            pass
        finally:
            self.alive = False
            for fut in self._waiters.values():
                if not fut.done():
                    fut.set_exception(ConnectionResetError("tcp connection lost"))
            self._waiters.clear()

    async def request(self, msg: Dict[str, Any], timeout: float = 3.0
                      ) -> Optional[Dict[str, Any]]:
        if not self.alive:
            return None
        loop = asyncio.get_running_loop()
        req_id = next(self._ids)
        msg = dict(msg, req_id=req_id)
        fut: asyncio.Future = loop.create_future()
        self._waiters[req_id] = fut
        try:
            self.writer.write(P.encode(msg))
            await self.writer.drain()
            return await asyncio.wait_for(fut, timeout)
        except Exception:
            self._waiters.pop(req_id, None)
            return None

    async def close(self) -> None:
        self.alive = False
        self._pump.cancel()
        with contextlib.suppress(Exception):
            self.writer.close()
            await self.writer.wait_closed()


async def open_tcp(edge_id: str, network_id: str,
                   timeout: float = 3.0) -> Optional[TcpConnection]:
    spec = EDGES[edge_id]
    port = pick_local_port(network_id)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(LOOPBACK, spec.relay_port,
                                    local_addr=(LOOPBACK, port)),
            timeout)
    except Exception:
        return None
    return TcpConnection(edge_id, network_id, reader, writer)


class TcpTransport:
    """Same interface as QuicTransport, minus everything that makes it good."""

    def __init__(self, telemetry=None) -> None:
        self.primary: Optional[TcpConnection] = None
        self.standby = None
        self.telemetry = telemetry
        self.reconnects = 0

    @property
    def edge_id(self) -> Optional[str]:
        return self.primary.edge_id if self.primary else None

    @property
    def network_id(self) -> Optional[str]:
        return self.primary.network_id if self.primary else None

    @property
    def alive(self) -> bool:
        return self.primary is not None and self.primary.alive

    async def open_primary(self, edge_id: str, network_id: str) -> bool:
        conn = await open_tcp(edge_id, network_id)
        if conn is None:
            return False
        if self.primary is not None:
            await self.primary.close()
        self.primary = conn
        return True

    async def migrate_primary(self, network_id: str) -> bool:
        # A TCP connection cannot follow you onto another network. This is the
        # limitation the whole experiment is designed to expose.
        return False

    async def request_primary(self, msg, timeout: float = 3.0):
        if self.primary is None:
            return None
        return await self.primary.request(msg, timeout)

    async def open_standby(self, edge_id: str, network_id: str) -> bool:
        return False

    async def request_standby(self, msg, timeout: float = 3.0):
        return None

    async def promote_standby(self) -> None:
        return None

    async def close_standby(self) -> None:
        return None

    async def drain(self, edge_id: str, session_id: str) -> None:
        return None

    async def close(self) -> None:
        if self.primary is not None:
            await self.primary.close()
            self.primary = None
