"""Event bus: one stream of facts, consumed by the dashboard and the evidence files.

Everything interesting the system does is emitted here - a request result, a
decision, a handoff phase, a verification. The dashboard subscribes live; the
JSONL file is what summaries, comparisons and replays are built from. Both see
identical events in the shape defined by `events.py`, so what the judges watch
and what the numbers say cannot disagree.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any, Callable, Dict, List, Optional

from . import events as E


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
    def __init__(self, path: Optional[str] = None, run_id: str = "",
                 controller_mode: str = "", scenario_id: str = "",
                 strict: bool = False) -> None:
        self.path = path
        self.run_id = run_id
        self.controller_mode = controller_mode
        self.scenario_id = scenario_id
        self.strict = strict
        self._fh = open(path, "a", buffering=1) if path else None
        self._subscribers: List[Callable[[dict], None]] = []
        self._queues: List[asyncio.Queue] = []
        self.start_ts = time.monotonic()
        self.count = 0
        self.schema_errors = 0

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._subscribers.append(fn)

    def subscribe_queue(self, maxsize: int = 500) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._queues.append(q)
        return q

    def unsubscribe_queue(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def elapsed(self) -> float:
        return time.monotonic() - self.start_ts

    def emit(self, event_type: str, source: str, **payload: Any) -> Dict[str, Any]:
        self.count += 1
        evt = {
            "run_id": self.run_id,
            "sequence_number": self.count,
            "elapsed_time": round(self.elapsed(), 4),
            "event_type": event_type,
            "controller_mode": self.controller_mode,
            "scenario_id": self.scenario_id,
            "source_component": source,
            "payload": json_safe(payload),
        }
        try:
            E.validate(evt)
        except E.SchemaError as exc:
            self.schema_errors += 1
            if self.strict:
                raise
            # Never silently drop a fact: record that it broke the contract.
            evt = {**evt, "event_type": "agent_error", "source_component": "runner",
                   "payload": {"error": f"schema: {exc}",
                               "original_event_type": event_type}}

        if self._fh is not None:
            self._fh.write(json.dumps(evt, default=str) + "\n")
        for fn in list(self._subscribers):
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

        Cancelling a run still lets its `finally` block emit mission_completed.
        Without this, that event lands in the *next* run's dashboard.
        """
        self._subscribers.clear()
        self._queues.clear()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
