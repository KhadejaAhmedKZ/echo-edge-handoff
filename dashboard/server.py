"""ECHO Inspection Mission - dashboard backend.

One process, one mission dashboard. It runs live experiments into the evidence
store, streams their telemetry to the browser, and serves every recorded run
for replay and download. The browser never computes a headline number itself:
every figure it shows comes from `metrics_updated` / `summary.json`, both
produced by echo_sim.metrics.

Run:  .venv/bin/python dashboard/server.py      (http://127.0.0.1:8080)
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from echo_sim import events as E  # noqa: E402
from echo_sim import evidence as EV  # noqa: E402
from echo_sim import scenarios as S  # noqa: E402
from echo_sim.config import APPLICATION_PROFILES, DEADLINE_THRESHOLDS_MS, RunConfig  # noqa: E402
from echo_sim.runner import describe_world, record_run  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
from dashboard.integrations import lifespan, fetch_json
app = FastAPI(title="ECHO Inspection Mission", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=os.path.join(HERE, "assets")), name="assets")

MODE_INFO = {
    E.TCP_RECONNECT: {
        "label": "TCP reconnect baseline",
        "summary": "Stays on one TCP connection until it breaks, then dials again "
                   "and rebuilds the session from nothing."},
    E.QUIC_FIXED: {
        "label": "QUIC with fixed server",
        "summary": "The QUIC connection follows the robot across networks, but "
                   "processing never leaves the first server."},
    E.MEASURED: {
        "label": "Measurement-driven migration",
        "summary": "ECHO moves the session using smoothed current measurements "
                   "(no forecast). Demonstration default."},
    E.PREDICTIVE: {
        "label": "Predictive migration",
        "summary": "ECHO moves the session using a 2-second trend forecast. "
                   "Experimental option."},
}
DEFAULT_MODE = E.MEASURED

ScenarioId = Literal[tuple(S.SCENARIOS)]            # type: ignore[valid-type]
ModeId = Literal[E.CONTROLLER_MODES]                # type: ignore[valid-type]
ProfileId = Literal[tuple(APPLICATION_PROFILES)]    # type: ignore[valid-type]


class RunRequest(BaseModel):
    scenario_id: ScenarioId = "gradual_coverage"
    controller_mode: ModeId = DEFAULT_MODE
    application_profile: ProfileId = "interactive_inspection"
    seed: int = Field(7, ge=0, le=999_999)
    duration_s: Optional[float] = Field(None, ge=20, le=180)
    request_rate_hz: float = Field(20.0, ge=5, le=30)
    deadline_ms: float = Field(150.0, ge=50, le=500)

    def to_config(self) -> RunConfig:
        scen = S.get(self.scenario_id)
        return RunConfig(mode=self.controller_mode, scenario_id=self.scenario_id,
                         profile=self.application_profile, seed=self.seed,
                         duration_s=self.duration_s or scen.default_duration_s,
                         frame_interval_s=1.0 / self.request_rate_hz,
                         deadline_ms=self.deadline_ms)


class ComparisonRequest(BaseModel):
    # Either assemble from existing runs ...
    run_ids: Optional[List[str]] = Field(None, max_length=4)
    # ... or run every mode now, one after another, under identical settings.
    scenario_id: ScenarioId = "gradual_coverage"
    application_profile: ProfileId = "interactive_inspection"
    seed: int = Field(7, ge=0, le=999_999)
    duration_s: Optional[float] = Field(None, ge=20, le=180)
    modes: List[ModeId] = Field(default_factory=lambda: list(E.CONTROLLER_MODES))
    note: str = Field("", max_length=300)


# ---------------------------------------------------------------------------
# Run manager: exactly one live experiment at a time (the relays and servers
# bind fixed local ports), each in its own evidence directory.
# ---------------------------------------------------------------------------


class LiveRun:
    def __init__(self, run_id: str, cfg: RunConfig) -> None:
        self.run_id = run_id
        self.cfg = cfg
        self.status = "running"
        self.events: List[dict] = []
        self.queues: List[asyncio.Queue] = []
        self.task: Optional[asyncio.Task] = None
        self.started = time.time()
        self.manifest: Optional[dict] = None

    def on_event(self, evt: dict) -> None:
        self.events.append(evt)
        for q in list(self.queues):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                with contextlib.suppress(Exception):
                    q.get_nowait()
                    q.put_nowait(evt)

    @property
    def latest_summary(self) -> Optional[dict]:
        for evt in reversed(self.events):
            if evt["event_type"] in ("mission_completed", "metrics_updated"):
                return evt["payload"]["summary"]
        return None


class Manager:
    def __init__(self) -> None:
        self.live: Optional[LiveRun] = None
        self.history: Dict[str, LiveRun] = {}
        self.followers: List[asyncio.Queue] = []    # /api/live/events
        self.job: Optional[dict] = None
        self._job_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self.live is not None and self.live.status == "running"

    async def start(self, cfg: RunConfig) -> LiveRun:
        async with self._lock:
            if self.busy:
                raise HTTPException(409, f"run {self.live.run_id} is still running")
            ready: asyncio.Future = asyncio.get_running_loop().create_future()
            holder: Dict[str, LiveRun] = {}

            def on_recorder(rec) -> None:
                lr = LiveRun(rec.run_id, cfg)
                holder["run"] = lr
                self.live = lr
                self.history[rec.run_id] = lr
                for q in self.followers:
                    with contextlib.suppress(Exception):
                        q.put_nowait({"event_type": "_live_run_changed",
                                      "run_id": rec.run_id})
                ready.set_result(lr)

            def fan_out(evt: dict) -> None:
                lr = holder.get("run")
                if lr is not None:
                    lr.on_event(evt)
                for q in list(self.followers):
                    with contextlib.suppress(asyncio.QueueFull):
                        q.put_nowait(evt)

            async def body() -> None:
                try:
                    m = await record_run(cfg, subscribers=[fan_out],
                                         on_recorder=on_recorder)
                    holder["run"].manifest = m
                    holder["run"].status = m["status"]
                except asyncio.CancelledError:
                    if "run" in holder:
                        holder["run"].status = "stopped"
                except Exception as exc:  # noqa: BLE001
                    if "run" in holder:
                        holder["run"].status = "error"
                        holder["run"].events.append({"event_type": "run_error",
                                                     "payload": {"reason": str(exc)}})
                    if not ready.done():
                        ready.set_exception(exc)
                finally:
                    lr = holder.get("run")
                    if lr is not None:
                        for q in list(lr.queues):
                            with contextlib.suppress(Exception):
                                q.put_nowait(None)

            task = asyncio.ensure_future(body())
            lr = await asyncio.wait_for(ready, 10.0)
            lr.task = task
            return lr

    async def stop(self, run_id: str) -> None:
        lr = self.history.get(run_id)
        if lr is None or lr.task is None or lr.task.done():
            raise HTTPException(409, "run is not running")
        lr.task.cancel()
        with contextlib.suppress(BaseException):
            await lr.task
        await asyncio.sleep(0.5)   # let sockets close before the next run binds

    async def run_comparison(self, req: ComparisonRequest) -> dict:
        if self.busy or (self._job_task and not self._job_task.done()):
            raise HTTPException(409, "an experiment is already running")
        job = {"job_id": f"job-{int(time.time())}", "status": "running",
               "scenario_id": req.scenario_id, "modes": list(req.modes),
               "run_ids": [], "current": None, "comparison_id": None, "error": None}
        self.job = job

        async def body() -> None:
            try:
                for mode in req.modes:
                    rr = RunRequest(scenario_id=req.scenario_id, controller_mode=mode,
                                    application_profile=req.application_profile,
                                    seed=req.seed, duration_s=req.duration_s)
                    lr = await self.start(rr.to_config())
                    job["current"] = lr.run_id
                    job["run_ids"].append(lr.run_id)
                    await lr.task
                    if lr.status != "completed":
                        raise RuntimeError(f"{mode} run ended as {lr.status}")
                    await asyncio.sleep(1.0)
                comp = EV.build_comparison(job["run_ids"], note=req.note)
                job["comparison_id"] = comp["comparison_id"]
                job["status"] = "completed"
            except asyncio.CancelledError:
                job["status"] = "stopped"
                if self.busy:
                    self.live.task.cancel()
            except Exception as exc:  # noqa: BLE001
                job["status"], job["error"] = "error", str(exc)
            finally:
                job["current"] = None

        self._job_task = asyncio.ensure_future(body())
        return job


mgr = Manager()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(HERE, "index.html"),
                        headers={"Cache-Control": "no-store"})


@app.get("/api/orbit/status")
async def orbit_status():
    return await asyncio.to_thread(fetch_json, '/api/satellite/status', 65)


@app.get("/api/orbit/constellation")
async def orbit_constellation():
    return await asyncio.to_thread(fetch_json, '/api/satellite/constellation', 35)


@app.get("/api/model/example")
async def model_example(sample: int = 0):
    if not 0 <= sample <= 100000:
        raise HTTPException(422, 'Invalid sample')
    return await asyncio.to_thread(fetch_json, '/api/ml/status?sample=' + str(sample), 8)


@app.get("/api/capabilities")
async def capabilities() -> JSONResponse:
    return JSONResponse({
        "product": "ECHO keeps a moving inspection application responsive by "
                   "selecting a suitable network and processing server, preparing "
                   "the target, and transferring session progress before switching.",
        "controller_modes": [{"id": m, **MODE_INFO[m]} for m in E.CONTROLLER_MODES],
        "default_mode": DEFAULT_MODE,
        "application_profiles": [{"id": k, **v} for k, v in APPLICATION_PROFILES.items()],
        "deadline_thresholds_ms": list(DEADLINE_THRESHOLDS_MS),
        "event_schema": E.SCHEMA_VERSION,
        "status": CAPABILITY_STATUS,
        "additional_experiments": ADDITIONAL_EXPERIMENTS,
    })


@app.get("/api/scenarios")
async def scenarios() -> JSONResponse:
    return JSONResponse({"scenarios": S.describe_all(), "world": describe_world(),
                         "default": S.DEFAULT_SCENARIO})


@app.get("/api/status")
async def status() -> JSONResponse:
    lr = mgr.live
    return JSONResponse({
        "live_run": ({"run_id": lr.run_id, "status": lr.status,
                      "controller_mode": lr.cfg.mode, "scenario_id": lr.cfg.scenario_id,
                      "started": lr.started} if lr else None),
        "busy": mgr.busy,
        "comparison_job": mgr.job,
    })


@app.post("/api/runs", status_code=202)
async def create_run(req: RunRequest) -> JSONResponse:
    lr = await mgr.start(req.to_config())
    return JSONResponse({"run_id": lr.run_id, "status": lr.status}, status_code=202)


@app.get("/api/runs")
async def runs() -> JSONResponse:
    listed = EV.list_runs()
    if mgr.busy:
        for r in listed:
            if r["run_id"] == mgr.live.run_id:
                r["status"] = "running"
    return JSONResponse({"runs": listed})


def _live(run_id: str) -> Optional[LiveRun]:
    lr = mgr.history.get(run_id)
    return lr if lr is not None and lr.status == "running" else None


def _exists(run_id: str) -> None:
    try:
        EV.run_dir(run_id)
    except FileNotFoundError:
        raise HTTPException(404, "no such run")


@app.get("/api/runs/{run_id}")
async def run_info(run_id: str) -> JSONResponse:
    _exists(run_id)
    lr = _live(run_id)
    conf = EV.read_json(run_id, "configuration.json")
    manifest = None if lr else EV.read_json(run_id, "manifest.json")
    return JSONResponse({"run_id": run_id,
                         "status": "running" if lr else manifest["status"],
                         "configuration": conf, "manifest": manifest})


@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str) -> JSONResponse:
    await mgr.stop(run_id)
    return JSONResponse({"run_id": run_id, "status": "stopped"})


@app.get("/api/runs/{run_id}/summary")
async def run_summary(run_id: str) -> JSONResponse:
    _exists(run_id)
    lr = _live(run_id)
    if lr:
        return JSONResponse({"run_id": run_id, "status": "running",
                             "summary": lr.latest_summary})
    return JSONResponse(EV.read_json(run_id, "summary.json"))


def _split(run_id: str, name: str, types) -> JSONResponse:
    _exists(run_id)
    lr = _live(run_id)
    if lr:
        rows = [e for e in lr.events if e["event_type"] in types]
    else:
        rows = EV.read_events(run_id, name)
    return JSONResponse({"run_id": run_id, "rows": rows})


@app.get("/api/runs/{run_id}/frames")
async def run_frames(run_id: str) -> JSONResponse:
    return _split(run_id, "frames.jsonl", E.FRAME_EVENTS)


@app.get("/api/runs/{run_id}/decisions")
async def run_decisions(run_id: str) -> JSONResponse:
    return _split(run_id, "decisions.jsonl", E.DECISION_EVENTS)


@app.get("/api/runs/{run_id}/handoffs")
async def run_handoffs(run_id: str) -> JSONResponse:
    return _split(run_id, "handoffs.jsonl", E.HANDOFF_EVENTS)


@app.get("/api/runs/{run_id}/recording")
async def run_recording(run_id: str) -> Response:
    """The complete event stream, for replay in the browser."""
    _exists(run_id)
    if _live(run_id):
        raise HTTPException(409, "run is still recording")
    return FileResponse(os.path.join(EV.run_dir(run_id), "events.jsonl"),
                        media_type="application/x-ndjson")


@app.get("/api/runs/{run_id}/export")
async def run_export(run_id: str) -> Response:
    _exists(run_id)
    if _live(run_id):
        raise HTTPException(409, "run is still recording")
    return Response(EV.export_zip(run_id), media_type="application/zip",
                    headers={"Content-Disposition":
                             f'attachment; filename="{run_id}.zip"'})


@app.get("/api/runs/{run_id}/verify")
async def run_verify(run_id: str) -> JSONResponse:
    _exists(run_id)
    return JSONResponse(EV.verify_run(run_id))


@app.post("/api/comparisons")
async def create_comparison(req: ComparisonRequest) -> JSONResponse:
    if req.run_ids:
        for rid in req.run_ids:
            _exists(rid)
        return JSONResponse(EV.build_comparison(req.run_ids, note=req.note))
    return JSONResponse(await mgr.run_comparison(req), status_code=202)


@app.get("/api/comparisons")
async def comparisons() -> JSONResponse:
    return JSONResponse({"comparisons": EV.list_comparisons()})


@app.get("/api/comparisons/{cid}")
async def comparison(cid: str) -> JSONResponse:
    try:
        return JSONResponse(EV.read_comparison(cid))
    except FileNotFoundError:
        raise HTTPException(404, "no such comparison")


async def _stream(sock: WebSocket, backlog: List[dict], q: asyncio.Queue) -> None:
    for evt in backlog:
        await sock.send_json(evt)
    while True:
        evt = await q.get()
        if evt is None:
            await sock.send_json({"event_type": "_stream_end"})
            return
        await sock.send_json(evt)


@app.websocket("/api/runs/{run_id}/events")
async def ws_run(sock: WebSocket, run_id: str) -> None:
    await sock.accept()
    lr = mgr.history.get(run_id)
    if lr is None:
        await sock.send_json({"event_type": "_error",
                              "message": "not a live run; use /recording to replay"})
        await sock.close()
        return
    q: asyncio.Queue = asyncio.Queue(maxsize=5000)
    backlog = list(lr.events)             # copy + register with no await between
    if lr.status == "running":
        lr.queues.append(q)
    else:
        q.put_nowait(None)
    try:
        await _stream(sock, backlog, q)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if q in lr.queues:
            lr.queues.remove(q)


@app.websocket("/api/live/events")
async def ws_live(sock: WebSocket) -> None:
    """Follow whichever run is live, including each run of a comparison."""
    await sock.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=5000)
    backlog = list(mgr.live.events) if mgr.busy else []
    mgr.followers.append(q)
    try:
        for evt in backlog:
            await sock.send_json(evt)
        while True:
            await sock.send_json(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if q in mgr.followers:
            mgr.followers.remove(q)


# ---------------------------------------------------------------------------
# What is real, emulated, simulated, separate or only proposed.
# ---------------------------------------------------------------------------

CAPABILITY_STATUS = [
    {"item": "QUIC transport and connection migration", "status": "implemented",
     "detail": "Real aioquic connections on loopback. The connection keeps its "
               "identity when the robot's network changes."},
    {"item": "TCP reconnect baseline", "status": "implemented",
     "detail": "Real TCP sockets; reconnects and rebuilds the session after a break."},
    {"item": "Network conditions", "status": "emulated",
     "detail": "Delay, jitter, loss and bandwidth applied by userspace relays "
               "(the tc/netem idea), driven by the scenario."},
    {"item": "Processing servers", "status": "simulated",
     "detail": "Four independent server services. Processing is a controlled "
               "delay that responds to load and cold starts; no detector model runs."},
    {"item": "Session-state transfer and continuity checks", "status": "implemented",
     "detail": "Session id, state version and last completed request are "
               "transferred and checked before and after the switch. The tracking "
               "content is synthetic, so no tracking accuracy is claimed."},
    {"item": "Four controller modes in one engine", "status": "implemented",
     "detail": "TCP reconnect, QUIC fixed server, measurement-driven and "
               "predictive migration; same scenario, workload and seed."},
    {"item": "Controlled failure: target cannot prepare", "status": "implemented",
     "detail": "Deterministic test event (scenario C), shown as a test event."},
    {"item": "Application profile", "status": "implemented",
     "detail": "Chosen explicitly. Automatic recognition of applications from "
               "encrypted traffic is not claimed; a traffic-shape classifier "
               "only advises and answers 'unknown' when unsure."},
    {"item": "Buffered playback workload", "status": "implemented",
     "detail": "A controlled segment workload with a measured buffer and stall "
               "counter. Not a real video player, and not YouTube."},
    {"item": "High-delay backup path (satellite-like)", "status": "emulated",
     "detail": "A link profile with satellite-like delay. No satellite terminal "
               "is controlled."},
    {"item": "Plain-language explanations", "status": "implemented",
     "detail": "Generated deterministically from the decision's own numbers. "
               "No language model is used."},
    {"item": "Orbital visibility (globe)", "status": "separate",
     "detail": "Additional experiment; does not drive this mission."},
    {"item": "macOS / Linux network control", "status": "separate",
     "detail": "Additional experiments; not integrated into this mission."},
    {"item": "Public Wi-Fi acquisition, captive portal, VPN", "status": "proposed",
     "detail": "Not implemented. This mission assumes managed infrastructure."},
]

ADDITIONAL_EXPERIMENTS = [
    {"name": "macOS network controller", "folder": "03_agentic-network-handoff_macos",
     "evidence": "Real network-service reorder verified on the dev MacBook; "
                 "no end-to-end handoff yet (needs a second simultaneous network)."},
    {"name": "Linux NetworkManager controller", "folder": "02_agentic-handoff_linux",
     "evidence": "Logic tested against mocked NetworkManager output; not yet run "
                 "on real Linux hardware."},
    {"name": "Wi-Fi switch utility", "folder": "04_wifi-switch",
     "evidence": "Real single-radio switch between two Wi-Fi networks; manual."},
    {"name": "QUIC migration spike", "folder": "05_netagent-quic-spike",
     "evidence": "Early transport feasibility check for connection migration."},
    {"name": "Satellite visibility dashboard", "folder": "06_satellite-dashboard",
     "evidence": "Orbital tracking and visibility; not connected to this mission."},
]


# ---------------------------------------------------------------------------
# Optional: launch the separate Wi-Fi switch prototype on this machine.
#
# A web page cannot run a program on a visitor's computer, and should not be
# able to. This only exists for the machine the dashboard is running on, and
# it is off unless you start with ECHO_ALLOW_LAUNCH=1.
#
# Three things keep it narrow: the command is fixed and takes no argument from
# the request, the server listens only on 127.0.0.1, and the POST requires a
# custom header, which a page on another site cannot send cross-origin without
# a CORS preflight this server never answers.
# ---------------------------------------------------------------------------

LAUNCH_ENABLED = os.environ.get("ECHO_ALLOW_LAUNCH") == "1"
PROJECT_ROOT = os.path.dirname(HERE)


def _wifi_switch_app() -> Optional[str]:
    """The working copy beside this project, else the copy vendored here."""
    for candidate in (
        os.path.join(os.path.dirname(PROJECT_ROOT), "04_wifi-switch", "app.py"),
        os.path.join(PROJECT_ROOT, "extras", "wifi-switch", "app.py"),
    ):
        if os.path.exists(candidate):
            return candidate
    return None


@app.get("/api/extras/wifi-switch")
async def wifi_switch_status() -> JSONResponse:
    path = _wifi_switch_app()
    return JSONResponse({
        "enabled": LAUNCH_ENABLED,
        "found": path is not None,
        "path": os.path.relpath(path, PROJECT_ROOT) if path else None,
        "reason": None if LAUNCH_ENABLED else
        "Launching is off. Restart with ECHO_ALLOW_LAUNCH=1 ./start_mission.sh",
    })


@app.post("/api/extras/wifi-switch/launch")
async def wifi_switch_launch(request: Request) -> JSONResponse:
    if not LAUNCH_ENABLED:
        raise HTTPException(403, "Launching is off. Restart with "
                                 "ECHO_ALLOW_LAUNCH=1 ./start_mission.sh")
    if request.headers.get("x-echo-launch") != "1":
        raise HTTPException(403, "Missing X-ECHO-Launch header.")
    path = _wifi_switch_app()
    if path is None:
        raise HTTPException(404, "The Wi-Fi switch prototype was not found "
                                 "beside this project or under extras/.")
    # The prototype is a Tkinter desktop app, so it opens its own window. Use
    # the interpreter its own run script uses; stdio is inherited so a failure
    # (a Python without Tkinter, say) is visible in this terminal.
    python = shutil.which("python3") or sys.executable
    proc = subprocess.Popen([python, os.path.basename(path)],
                            cwd=os.path.dirname(path))
    return JSONResponse({"launched": True, "pid": proc.pid,
                         "path": os.path.relpath(path, PROJECT_ROOT)})


def main() -> None:
    import uvicorn
    EV.write_configurations()
    port = int(os.environ.get("ECHO_PORT", "8080"))
    print(f"ECHO Inspection Mission: http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
