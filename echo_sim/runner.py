"""Boot the whole world for one experiment run and tear it down cleanly.

Every controller mode gets the same four servers, the same four network
profiles, the same scenario (route, disturbances, faults), the same seed and
the same workload. The only thing that varies is the controller's decision
logic. That is what makes the numbers at the end comparable.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import asdict, replace
from typing import Any, Dict, List, Optional, Tuple

from . import events as E
from . import scenarios as S
from .client import EchoController
from .config import (APPLICATION_PROFILES, EDGES, NETWORKS, RunConfig,
                     backhaul_ms)
from .edge import start_all_edges
from .metrics import RunMetrics
from .netem import start_tcp_relays, start_udp_relays
from .telemetry import Telemetry
from .transport import QuicTransport, TcpTransport
from .world import ZONES, World

# Where on the site the robot physically crosses between zones. Used to score
# every mode over identical windows.
CROSSING_POSITIONS = [(0.26, 0.46), (0.56, 0.74), (0.78, 0.94)]


def crossing_windows(route: Tuple[float, float], duration_s: float
                     ) -> List[Tuple[float, float]]:
    """Map site-position crossings onto mission time for this route."""
    a, b = route
    if b <= a:
        return []
    out = []
    for lo, hi in CROSSING_POSITIONS:
        lo, hi = max(lo, a), min(hi, b)
        if hi > lo:
            out.append(((lo - a) / (b - a) * duration_s,
                        (hi - a) / (b - a) * duration_s))
    return out


def describe_world() -> Dict[str, Any]:
    """Static description of networks, servers and zones, for the map."""
    return {
        "networks": {nid: {"label": n.label, "colour": n.colour,
                           "best": list(n.best), "worst": list(n.worst),
                           "coverage": [list(k) for k in n.coverage],
                           "emulated": True}
                     for nid, n in NETWORKS.items()},
        "servers": {eid: {"label": e.label, "home_network": e.home_network,
                          "base_processing_ms": e.base_inference_ms,
                          "load_penalty_ms": e.load_penalty_ms,
                          "site_position": e.site_position}
                    for eid, e in EDGES.items()},
        "backhaul_ms": {nid: {eid: backhaul_ms(nid, eid) for eid in EDGES}
                        for nid in NETWORKS},
        "zones": [[p, name] for p, name in ZONES],
    }


def run_configuration(cfg: RunConfig) -> Dict[str, Any]:
    """Everything needed to reproduce a run, as recorded in configuration.json."""
    scen = S.get(cfg.scenario_id)
    return {
        "controller_mode": E.canonical_mode(cfg.mode),
        "scenario": scen.as_dict(),
        "scenario_id": scen.id,
        "scenario_version": scen.version,
        "application_profile": cfg.profile,
        "application_profile_label": APPLICATION_PROFILES[cfg.profile]["label"],
        "seed": cfg.seed,
        "duration_s": cfg.duration_s,
        "request_rate_hz": cfg.request_rate_hz,
        "deadline_ms": cfg.deadline_ms,
        "deadline_note": "analysis threshold only; never changes controller behaviour",
        "prediction_horizon_s": (0.0 if E.canonical_mode(cfg.mode) == E.MEASURED
                                 else cfg.prediction_horizon_s),
        "controller": {k: v for k, v in asdict(cfg).items()
                       if k not in ("telemetry_path",)},
        "world": describe_world(),
        "crossing_windows_s": crossing_windows(scen.route, cfg.duration_s),
        "status": {
            "network": "emulated (userspace delay/jitter/loss/bandwidth relays)",
            "processing": "simulated (controlled delay responding to load and warm/cold state)",
            "transport": "real (aioquic QUIC with connection migration; real TCP)",
            "tracking_state": "synthetic (moving placeholder tracks)",
        },
    }


async def run_experiment(
    cfg: Optional[RunConfig] = None,
    telemetry: Optional[Telemetry] = None,
    on_ready=None,
    *,
    mode: Optional[str] = None,
    duration_s: Optional[float] = None,
    seed: Optional[int] = None,
    telemetry_path: Optional[str] = None,
    fail_edge_prepare: Optional[str] = None,
) -> Tuple[RunMetrics, Dict[str, Any]]:
    cfg = cfg or RunConfig()
    overrides = {k: v for k, v in (("mode", mode), ("duration_s", duration_s),
                                   ("seed", seed)) if v is not None}
    cfg = replace(cfg, **overrides)
    mode_c = E.canonical_mode(cfg.mode)
    cfg = replace(cfg, mode=mode_c)
    scen = S.get(cfg.scenario_id)

    own_telemetry = telemetry is None
    tel = telemetry or Telemetry(
        telemetry_path, run_id=f"{scen.id}-{mode_c}-{int(time.time())}",
        controller_mode=mode_c, scenario_id=scen.id)
    tel.controller_mode, tel.scenario_id = mode_c, scen.id
    tel.emit("mission_started", "runner", scenario_id=scen.id,
             scenario_version=scen.version, controller_mode=mode_c,
             application_profile=cfg.profile, seed=cfg.seed,
             duration_s=cfg.duration_s, request_rate_hz=cfg.request_rate_hz,
             deadline_ms=cfg.deadline_ms, route=list(scen.route),
             disturbances=[asdict(d) for d in scen.disturbances],
             faults=[asdict(f) for f in scen.faults])

    world = World(cfg.duration_s, cfg.seed, route=scen.route,
                  disturbances=scen.disturbances)
    injector = S.FaultInjector(scen.faults, tel)
    services, servers = await start_all_edges(cfg, tel, faults=injector)
    if fail_edge_prepare:
        services[fail_edge_prepare].fail_prepare = True

    relays: Any
    if mode_c == E.TCP_RECONNECT:
        relays = await start_tcp_relays(world, cfg.seed)
        transport = TcpTransport(tel)
    else:
        relays = await start_udp_relays(world, cfg.seed)
        transport = QuicTransport(tel)

    controller = EchoController(cfg, world, services, transport, tel)
    controller.metrics.crossing_windows = crossing_windows(scen.route, cfg.duration_s)

    metrics = controller.metrics
    error: Optional[str] = None
    try:
        started = await controller.start()
        if not started:
            error = "could not open the initial session"
            tel.emit("run_error", "runner", reason=error)
            return metrics, {"error": error}
        if on_ready is not None:
            on_ready(controller)
        metrics = await controller.run()
    finally:
        with contextlib.suppress(Exception):
            await transport.close()
        for relay in getattr(relays, "values", lambda: [])():
            with contextlib.suppress(Exception):
                relay.close()
        for server in servers:
            with contextlib.suppress(Exception):
                server.close()
        await asyncio.sleep(0.25)
        summary = metrics.summary()
        tel.emit("mission_completed", "runner", summary=summary,
                 faults_fired=injector.fired, error=error,
                 schema_errors=tel.schema_errors)
        if own_telemetry:
            tel.close()

    return metrics, metrics.summary()


async def record_run(cfg: RunConfig, subscribers=(), on_ready=None,
                     on_recorder=None, strict: bool = False) -> Dict[str, Any]:
    """Run one experiment into its own evidence directory. Returns the manifest.

    `subscribers` receive every event live (the dashboard); the same events go
    to events.jsonl. A cancelled run is still finalised, with status "stopped".
    """
    from . import evidence as EV

    EV.write_configurations()
    cfg = replace(cfg, mode=E.canonical_mode(cfg.mode))
    S.get(cfg.scenario_id)
    rec = EV.RunRecorder(cfg)
    if on_recorder is not None:
        on_recorder(rec)
    rec.write_configuration(run_configuration(cfg))
    tel = Telemetry(rec.events_path, run_id=rec.run_id, controller_mode=cfg.mode,
                    scenario_id=cfg.scenario_id, strict=strict)
    for fn in subscribers:
        tel.subscribe(fn)
    status = "completed"
    summary: Dict[str, Any] = {}
    try:
        metrics, summary = await run_experiment(cfg, telemetry=tel, on_ready=on_ready)
        if "error" in summary:
            status = "error"
    except asyncio.CancelledError:
        status = "stopped"
        raise
    except Exception as exc:  # noqa: BLE001
        status = "error"
        summary = {"error": f"{type(exc).__name__}: {exc}"}
        raise
    finally:
        tel.detach()
        tel.close()
        if not summary:
            # Cancelled mid-run: take the numbers from the last recorded event.
            for evt in reversed(list(EV._read_jsonl(rec.events_path))):
                if evt["event_type"] in ("mission_completed", "metrics_updated"):
                    summary = evt["payload"]["summary"]
                    break
        manifest = rec.finalize(summary, status)
    return manifest
