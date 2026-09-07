"""Static configuration for the ECHO prototype.

Four access networks, four edge sites, one mobile user carrying a laptop.
Everything here is data: tuning the demo means editing this file, not the logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

# --------------------------------------------------------------------------
# Access networks
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NetworkSpec:
    """One access network, described at its best and worst.

    Live characteristics are interpolated between `best` and `worst` using the
    coverage quality at the user's current position (see world.py).
    """

    id: str
    label: str
    colour: str
    # (rtt_ms, jitter_ms, loss_ratio, bandwidth_mbps)
    best: Tuple[float, float, float, float]
    worst: Tuple[float, float, float, float]
    # coverage keyframes: (position 0..1, quality 0..1); quality 0 == unusable
    coverage: List[Tuple[float, float]]
    metered: bool = False
    trusted: bool = True


NETWORKS: Dict[str, NetworkSpec] = {
    "wifi": NetworkSpec(
        id="wifi",
        label="Indoor Wi-Fi",
        colour="#c4b5fd",
        best=(12.0, 2.0, 0.0005, 300.0),
        worst=(180.0, 35.0, 0.18, 8.0),
        coverage=[(0.00, 1.00), (0.18, 0.96), (0.30, 0.58),
                  (0.42, 0.16), (0.52, 0.00), (1.00, 0.00)],
    ),
    "cellular": NetworkSpec(
        id="cellular",
        label="Private 5G",
        colour="#8b5cf6",
        best=(22.0, 3.0, 0.0008, 200.0),
        worst=(120.0, 25.0, 0.08, 10.0),
        coverage=[(0.00, 0.42), (0.16, 0.68), (0.34, 0.95), (0.52, 0.88),
                  (0.66, 0.44), (0.80, 0.22), (1.00, 0.18)],
        metered=True,
    ),
    "satellite": NetworkSpec(
        id="satellite",
        label="Satellite",
        colour="#ff7a2f",
        # Even at full quality satellite is slow: availability != suitability.
        best=(520.0, 25.0, 0.004, 60.0),
        worst=(780.0, 60.0, 0.04, 8.0),
        coverage=[(0.00, 0.52), (0.45, 0.62), (0.72, 0.70), (1.00, 0.60)],
        metered=True,
    ),
    "wired": NetworkSpec(
        id="wired",
        label="Wired dock",
        colour="#ede9fe",
        best=(1.5, 0.2, 0.00001, 1000.0),
        worst=(6.0, 1.5, 0.001, 400.0),
        coverage=[(0.00, 0.00), (0.80, 0.00), (0.86, 0.92), (1.00, 1.00)],
    ),
}

NETWORK_ORDER = ["wifi", "cellular", "satellite", "wired"]

# --------------------------------------------------------------------------
# Edge sites
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EdgeSpec:
    id: str
    label: str
    home_network: str
    # simulated inference cost for one frame on an idle box
    base_inference_ms: float
    # extra ms per concurrent session already being served
    load_penalty_ms: float
    # where the site physically sits along the route, for proximity cost
    site_position: float
    quic_port: int
    tcp_port: int
    relay_port: int


EDGES: Dict[str, EdgeSpec] = {
    "A": EdgeSpec("A", "Edge A - indoor", "wifi", 26.0, 4.0, 0.10, 4441, 5551, 4451),
    "B": EdgeSpec("B", "Edge B - yard 5G", "cellular", 21.0, 4.0, 0.40, 4442, 5552, 4452),
    "C": EdgeSpec("C", "Edge C - remote", "satellite", 15.0, 3.0, 0.70, 4443, 5553, 4453),
    "D": EdgeSpec("D", "Edge D - dock", "wired", 12.0, 3.0, 0.95, 4444, 5554, 4454),
}

EDGE_ORDER = ["A", "B", "C", "D"]

# Local UDP/TCP port the client binds per access network. The impairment relay
# reads the client's source port to decide which network profile to apply, which
# is also what makes real QUIC connection migration observable.
CLIENT_PORT_BASE: Dict[str, int] = {
    "wifi": 39000,
    "cellular": 39100,
    "satellite": 39200,
    "wired": 39300,
}
# A span, not a single port, so the primary and a standby connection can both
# be attached to the same access network without colliding.
CLIENT_PORT_SPAN = 40
CLIENT_PORTS = CLIENT_PORT_BASE  # backwards-compatible alias

# Extra one-way transit once you leave an edge's home network and have to reach
# it across the operator backhaul. This is what makes "the connection survived"
# insufficient: you can still be talking to a box that is now far away.
BACKHAUL_MS = 22.0
BACKHAUL_MS_SATELLITE = 45.0


def backhaul_ms(access_network: str, edge_id: str) -> float:
    edge = EDGES[edge_id]
    if edge.home_network == access_network:
        return 0.0
    if "satellite" in (access_network, edge.home_network):
        return BACKHAUL_MS_SATELLITE
    return BACKHAUL_MS


# --------------------------------------------------------------------------
# Decision weights
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Weights:
    """Score(edge) = sum(w_i * term_i); lower is better."""

    network_latency: float = 1.0
    jitter: float = 2.5
    packet_loss: float = 300.0      # loss ratio -> ms-equivalent penalty
    inference_latency: float = 1.0
    edge_load: float = 6.0
    proximity: float = 30.0


# Per-activity weighting. A call cares about steadiness, a bulk transfer cares
# about throughput, inference cares about total end-to-end time.
INTENT_WEIGHTS: Dict[str, Weights] = {
    "inference": Weights(),
    "realtime_call": Weights(network_latency=1.0, jitter=6.0, packet_loss=600.0,
                             inference_latency=0.4, edge_load=3.0, proximity=10.0),
    "bulk_transfer": Weights(network_latency=0.3, jitter=0.5, packet_loss=120.0,
                             inference_latency=0.3, edge_load=2.0, proximity=5.0),
    "streaming": Weights(network_latency=0.5, jitter=2.0, packet_loss=200.0,
                          inference_latency=0.5, edge_load=3.0, proximity=10.0),
}

# --------------------------------------------------------------------------
# Run parameters
# --------------------------------------------------------------------------


@dataclass
class RunConfig:
    mode: str = "echo"              # tcp | quic | echo
    duration_s: float = 90.0
    frame_interval_s: float = 0.05  # 20 frames/second
    session_id: str = "robot-01"
    model_id: str = "inspection-model-v1"
    model_version: str = "1.0"
    # Predictor looks this far ahead when scoring candidate edges.
    prediction_horizon_s: float = 2.0
    # Decision hysteresis: a challenger must be this much better ...
    switch_margin: float = 0.12
    # ... for this many consecutive ticks before a handoff is authorised.
    switch_patience: int = 3
    agent_tick_s: float = 0.25
    # Cold-start penalty when an edge has to rebuild tracking state from zero.
    warmup_frames: int = 12
    warmup_penalty_ms: float = 55.0
    tcp_reconnect_timeout_s: float = 1.2
    seed: int = 7
    telemetry_path: str = ""
