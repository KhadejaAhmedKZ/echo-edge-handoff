"""Event bus: one stream of facts, consumed by the dashboard and the log file.

Everything interesting the system does is emitted here - a frame result, an
agent decision, a handoff step, a diagnosis. The dashboard subscribes live; the
JSONL file is what the graphs are built from afterwards. Both see identical
data, so what the judges watch and what the numbers say cannot disagree.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any, Callable, Dict, List, Optional


def json_safe(value: Any) -> Any:
    """Replace inf/NaN with None, recursively.

    Python's json module happily writes `Infinity`, which is not valid JSON and
    which every browser refuses to parse. An unreachable network legitimately
    has infinite latency, so this case is normal, not exceptional.
    """
    if isinstance(value, float):
        return None if (math.isinf(value) or math.isnan(value)) else value
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


class Telemetry:
    def __init__(self, path: Optional[str] = None, run_id: str = "") -> None:
        self.path = path
        self.run_id = run_id
        self._fh = open(path, "a", buffering=1) if path else None
        self._subscribers: List[Callable[[dict], None]] = []
        self._queues: List[asyncio.Queue] = []
        self.start_ts = time.time()
        self.count = 0

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._subscribers.append(fn)

    def subscribe_queue(self, maxsize: int = 500) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._queues.append(q)
        return q

    def unsubscribe_queue(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def emit(self, kind: str, **fields: Any) -> Dict[str, Any]:
        evt = {
            "kind": kind,
            "run_id": self.run_id,
            "t": round(time.time() - self.start_ts, 4),
            "wall": time.time(),
        }
        evt.update(json_safe(fields))
        self.count += 1

        if self._fh is not None:
            self._fh.write(json.dumps(evt, default=str) + "\n")
        for fn in self._subscribers:
            try:
                fn(evt)
            except Exception:
                pass
        for q in list(self._queues):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                # A slow dashboard must never back-pressure the experiment.
                try:
                    q.get_nowait()
                    q.put_nowait(evt)
                except Exception:
                    pass
        return evt

    def detach(self) -> None:
        """Stop feeding subscribers, without closing the log file yet.

        Cancelling a run still lets its `finally` block emit a run_end. Without
        this, that event lands in the *next* run's dashboard and reports the
        wrong numbers.
        """
        self._subscribers.clear()
        self._queues.clear()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
