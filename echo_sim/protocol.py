"""The ECHO application protocol.

ECHO rides on top of QUIC (or plain TCP, for the baseline). QUIC keeps the
*transport* alive across a network change; ECHO moves the *compute session*
across an edge change. They solve different halves of the same problem, which
is the whole argument of this project.

Wire format is deliberately boring: 4-byte big-endian length, then JSON. Easy
to read in Wireshark, easy to swap for a binary encoding later.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

# -- message types ---------------------------------------------------------

ECHO_INIT = "ECHO_INIT"          # open an inference session on an edge
ECHO_FRAME = "ECHO_FRAME"        # one camera frame to infer on
ECHO_RESULT = "ECHO_RESULT"      # inference result for a frame
ECHO_METRICS = "ECHO_METRICS"    # edge reports its own health
ECHO_PREPARE = "ECHO_PREPARE"    # ask a target edge to stage a session
ECHO_STATE = "ECHO_STATE"        # transfer the live session state
ECHO_READY = "ECHO_READY"        # target confirms the session is reconstructed
ECHO_VERIFY = "ECHO_VERIFY"      # duplicate frame, proves the target really works
ECHO_COMMIT = "ECHO_COMMIT"      # target becomes the active inference server
ECHO_ACK = "ECHO_ACK"            # handoff complete
ECHO_DRAIN = "ECHO_DRAIN"        # old edge: finish outstanding work, take no more
ECHO_RECOVER = "ECHO_RECOVER"    # handoff failed, fall back
ECHO_ERROR = "ECHO_ERROR"

PROTOCOL_VERSION = 1


# -- session state ---------------------------------------------------------


@dataclass
class SessionState:
    """Everything a target edge needs to continue an inference session.

    Note what is *not* here: the model. Every edge already has it. Only the
    live session travels, which is why a handoff costs milliseconds instead of
    a model download.
    """

    session_id: str
    model_id: str
    model_version: str
    last_processed_frame: int = 0
    state_version: int = 0
    config: Dict[str, Any] = field(default_factory=dict)
    tracking_state: Dict[str, Any] = field(default_factory=dict)
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)

    def bump(self, frame_no: int, tracking: Dict[str, Any]) -> None:
        self.last_processed_frame = max(self.last_processed_frame, frame_no)
        self.state_version += 1
        self.tracking_state = tracking
        self.updated_ts = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SessionState":
        return cls(**d)

    def size_bytes(self) -> int:
        return len(json.dumps(self.to_dict()).encode())


# -- message helpers -------------------------------------------------------


def message(msg_type: str, **fields: Any) -> Dict[str, Any]:
    m = {"v": PROTOCOL_VERSION, "type": msg_type, "ts": time.time()}
    m.update(fields)
    return m


def encode(msg: Dict[str, Any]) -> bytes:
    payload = json.dumps(msg, separators=(",", ":")).encode()
    return len(payload).to_bytes(4, "big") + payload


class FrameDecoder:
    """Incremental length-prefixed decoder; feed it bytes, get messages."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes):
        self._buf.extend(data)
        while len(self._buf) >= 4:
            size = int.from_bytes(self._buf[:4], "big")
            if len(self._buf) < 4 + size:
                return
            payload = bytes(self._buf[4:4 + size])
            del self._buf[:4 + size]
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue


async def read_message(reader: asyncio.StreamReader) -> Optional[Dict[str, Any]]:
    """Read one length-prefixed message from a TCP stream."""
    try:
        header = await reader.readexactly(4)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
    size = int.from_bytes(header, "big")
    try:
        payload = await reader.readexactly(size)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None
