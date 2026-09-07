"""Dashboard server: runs an experiment and streams it to the browser live.

The browser gets exactly the same event stream that the log file gets, so what
the 3D view shows and what the numbers say cannot drift apart. It can also
replay a finished run from its JSONL file, which is the safe way to demo when
you do not want to depend on live timing.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from echo_sim.runner import run_experiment  # noqa: E402
from echo_sim.telemetry import Telemetry  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = os.path.join(ROOT, "results")

app = FastAPI(title="ECHO dashboard")


class Hub:
    """Fan-out of one telemetry stream to every connected browser."""

    def __init__(self) -> None:
        self.clients: List[asyncio.Queue] = []
        self.task: Optional[asyncio.Task] = None
        self.telemetry: Optional[Telemetry] = None
        self.last_state: Optional[dict] = None
        self.mode: Optional[str] = None
        self.summary: Optional[dict] = None

    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.clients.append(q)
        return q

    def unregister(self, q: asyncio.Queue) -> None:
        if q in self.clients:
            self.clients.remove(q)

    def broadcast(self, evt: dict) -> None:
        if evt.get("kind") == "state":
            self.last_state = evt
        if evt.get("kind") == "run_end":
            self.summary = evt
        for q in list(self.clients):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                with contextlib.suppress(Exception):
                    q.get_nowait()
                    q.put_nowait(evt)

    async def start_run(self, mode: str, duration: float, seed: int,
                        fail_edge: Optional[str] = None) -> None:
        await self.stop_run()
        self.mode = mode
        self.summary = None
        os.makedirs(RESULTS, exist_ok=True)
        tel = Telemetry(os.path.join(RESULTS, f"telemetry-{mode}.jsonl"),
                        run_id=f"{mode}-live")
        tel.subscribe(self.broadcast)
        self.telemetry = tel
        self.task = asyncio.ensure_future(
            run_experiment(mode=mode, duration_s=duration, seed=seed,
                           telemetry=tel, fail_edge_prepare=fail_edge))

    async def stop_run(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(Exception):
                await self.task
        self.task = None
        if self.telemetry is not None:
            self.telemetry.close()
            self.telemetry = None
        await asyncio.sleep(0.4)

    async def replay(self, mode: str, speed: float = 1.0) -> None:
        """Play a recorded run back at wall-clock speed."""
        await self.stop_run()
        path = os.path.join(RESULTS, f"telemetry-{mode}.jsonl")
        if not os.path.exists(path):
            self.broadcast({"kind": "error", "message": f"no recording for {mode}"})
            return

        async def _play() -> None:
            with open(path) as fh:
                events = [json.loads(line) for line in fh if line.strip()]
            t0 = events[0]["t"] if events else 0.0
            start = asyncio.get_running_loop().time()
            for evt in events:
                target = start + (evt["t"] - t0) / max(speed, 0.05)
                delay = target - asyncio.get_running_loop().time()
                if delay > 0:
                    await asyncio.sleep(delay)
                self.broadcast(evt)

        self.mode = mode
        self.task = asyncio.ensure_future(_play())


hub = Hub()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(HERE, "index.html"))


@app.get("/api/status")
async def status() -> JSONResponse:
    return JSONResponse({
        "running": hub.task is not None and not hub.task.done(),
        "mode": hub.mode,
        "summary": hub.summary,
        "clients": len(hub.clients),
    })


@app.get("/api/results")
async def results() -> JSONResponse:
    out: Dict[str, Any] = {}
    for mode in ("tcp", "quic", "echo"):
        path = os.path.join(RESULTS, f"run-{mode}.json")
        if os.path.exists(path):
            with open(path) as fh:
                out[mode] = json.load(fh)["summary"]
    return JSONResponse(out)


@app.websocket("/ws")
async def ws(sock: WebSocket) -> None:
    await sock.accept()
    q = hub.register()
    if hub.last_state is not None:
        await sock.send_json(hub.last_state)

    async def pump() -> None:
        while True:
            evt = await q.get()
            await sock.send_json(evt)

    pump_task = asyncio.ensure_future(pump())
    try:
        while True:
            msg = await sock.receive_json()
            cmd = msg.get("cmd")
            if cmd == "start":
                await hub.start_run(msg.get("mode", "echo"),
                                    float(msg.get("duration", 90)),
                                    int(msg.get("seed", 7)),
                                    msg.get("fail_edge"))
            elif cmd == "stop":
                await hub.stop_run()
            elif cmd == "replay":
                await hub.replay(msg.get("mode", "echo"),
                                 float(msg.get("speed", 1.0)))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        pump_task.cancel()
        hub.unregister(q)


def main() -> None:
    import uvicorn
    port = int(os.environ.get("ECHO_PORT", "8080"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
