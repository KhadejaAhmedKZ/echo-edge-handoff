"""Boot the whole world for one experiment run and tear it down cleanly.

Every mode gets the same four edge sites, the same four network profiles, the
same route timing, the same seed and the same frame workload. The only thing
that varies is the client's decision logic. That is what makes the three
numbers at the end comparable.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import Any, Dict, Optional, Tuple

from .client import EchoController
from .config import RunConfig
from .edge import start_all_edges
from .metrics import RunMetrics
from .netem import start_tcp_relays, start_udp_relays
from .telemetry import Telemetry
from .transport import QuicTransport, TcpTransport
from .world import World

# Where along the route the user is physically crossing between zones. Used to
# score every mode over identical windows.
CROSSING_POSITIONS = [(0.26, 0.46), (0.56, 0.74), (0.78, 0.94)]


async def run_experiment(
    mode: str = "echo",
    duration_s: float = 90.0,
    seed: int = 7,
    telemetry_path: Optional[str] = None,
    telemetry: Optional[Telemetry] = None,
    frame_interval_s: float = 0.05,
    fail_edge_prepare: Optional[str] = None,
    on_ready=None,
) -> Tuple[RunMetrics, Dict[str, Any]]:
    cfg = RunConfig(mode=mode, duration_s=duration_s, seed=seed,
                    frame_interval_s=frame_interval_s)
    own_telemetry = telemetry is None
    tel = telemetry or Telemetry(telemetry_path, run_id=f"{mode}-{int(time.time())}")
    tel.emit("run_begin", mode=mode, duration_s=duration_s, seed=seed)

    world = World(duration_s, seed)
    services, servers = await start_all_edges(cfg, tel)

    if fail_edge_prepare:
        # Deliberately break one target so the Recovery Agent has something to do.
        services[fail_edge_prepare].fail_prepare = True

    relays: Any
    if mode == "tcp":
        relays = await start_tcp_relays(world, seed)
        transport = TcpTransport(tel)
    else:
        relays = await start_udp_relays(world, seed)
        transport = QuicTransport(tel)

    controller = EchoController(cfg, world, services, transport, tel)
    controller.metrics.crossing_windows = [
        (a * duration_s, b * duration_s) for a, b in CROSSING_POSITIONS]

    metrics = controller.metrics
    try:
        started = await controller.start()
        if not started:
            tel.emit("run_error", reason="could not open the initial session")
            return metrics, {"error": "startup failed"}
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
        tel.emit("run_end", **summary)
        if own_telemetry:
            tel.close()

    return metrics, metrics.summary()


async def run_all(duration_s: float = 90.0, seed: int = 7,
                  out_dir: str = "results") -> Dict[str, Dict[str, Any]]:
    """Run tcp, quic and echo back to back under identical conditions."""
    os.makedirs(out_dir, exist_ok=True)
    results: Dict[str, Dict[str, Any]] = {}
    for mode in ("tcp", "quic", "echo"):
        print(f"\n=== running {mode} ===", flush=True)
        metrics, summary = await run_experiment(
            mode=mode, duration_s=duration_s, seed=seed,
            telemetry_path=os.path.join(out_dir, f"telemetry-{mode}.jsonl"))
        metrics.to_json(os.path.join(out_dir, f"run-{mode}.json"))
        results[mode] = summary
        for k in ("frames_sent", "frames_delivered", "frames_lost", "p95_ms",
                  "p95_crossing_ms", "p95_settled_ms", "zone_gap_ms",
                  "longest_gap_ms", "handoffs", "reconnects",
                  "time_on_suboptimal_edge_s"):
            print(f"  {k:28s} {summary.get(k)}")
        # Let sockets settle before the next run rebinds the same ports.
        await asyncio.sleep(1.0)
    return results
