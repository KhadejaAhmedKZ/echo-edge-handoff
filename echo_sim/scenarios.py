"""Mission scenarios.

A scenario is data: which stretch of the route the robot covers, what
disturbances hit which network and when, and which controlled faults are
injected. Every controller mode runs against the same scenario definition, so
the comparison is fair by construction.

Disturbance and fault times are fractions of the mission duration, so a
scenario means the same thing at 60 s and at 90 s.

Each definition carries a version. Change anything that alters behaviour and
bump the version: the evidence manifest records it.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Disturbance:
    network_id: str
    start: float                 # fraction of mission time
    end: float
    rtt_add_ms: float = 0.0
    jitter_add_ms: float = 0.0
    loss_add: float = 0.0
    outage: bool = False         # network unusable for the whole window
    label: str = ""


@dataclass(frozen=True)
class Fault:
    """A deliberate, labelled test event. Never an unexpected real failure."""

    type: str                    # currently: target_prepare_failure
    count: int = 1               # how many times it fires
    server: str = "any"          # "any" = whichever server is prepared first
    label: str = ""


@dataclass(frozen=True)
class Scenario:
    id: str
    version: int
    title: str
    summary: str
    tests: str
    route: Tuple[float, float] = (0.0, 1.0)
    default_duration_s: float = 90.0
    disturbances: Tuple[Disturbance, ...] = ()
    faults: Tuple[Fault, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["route"] = list(self.route)
        return d


SCENARIOS: Dict[str, Scenario] = {
    "gradual_coverage": Scenario(
        id="gradual_coverage",
        version=1,
        title="A - Gradual coverage change",
        summary=("The robot walks from the indoor lab, through the yard, to the "
                 "dock. Indoor Wi-Fi fades gradually while private 5G and later "
                 "the wired dock become useful."),
        tests="The core mechanism: prepare another server and move the session "
              "before the current path becomes unusable.",
        route=(0.0, 1.0),
        default_duration_s=90.0,
    ),
    "brief_disturbance": Scenario(
        id="brief_disturbance",
        version=1,
        title="B - Brief disturbance",
        summary=("The robot inspects inside the lab, where Wi-Fi is strong. For "
                 "about 4 seconds Wi-Fi suffers a delay and loss spike, then "
                 "recovers fully."),
        tests="Whether the controller avoids switching away and straight back "
              "for a problem that fixes itself.",
        route=(0.02, 0.16),
        default_duration_s=60.0,
        disturbances=(
            Disturbance("wifi", 0.45, 0.52, rtt_add_ms=140.0, jitter_add_ms=20.0,
                        loss_add=0.06,
                        label="Test event: Wi-Fi delay and loss spike"),
        ),
    ),
    "prepare_failure": Scenario(
        id="prepare_failure",
        version=1,
        title="C - Target cannot prepare",
        summary=("Same route as scenario A, but the first time a target server "
                 "is asked to prepare the session, it refuses."),
        tests="Whether the controller keeps serving from the old server, reports "
              "the failure, and only switches once a target is verified.",
        route=(0.0, 1.0),
        default_duration_s=90.0,
        faults=(
            Fault("target_prepare_failure", count=1, server="any",
                  label="Test event: target preparation failure"),
        ),
    ),
    "backup_only": Scenario(
        id="backup_only",
        version=1,
        title="D - Only the backup path is left",
        summary=("In the yard, private 5G is cut for a quarter of a minute. The "
                 "only remaining route is the emulated high-delay "
                 "(satellite-like) backup path."),
        tests="That ECHO ignores the backup path while a better one exists, uses "
              "it when it is the only route, and that staying reachable does not "
              "mean meeting a 150 ms deadline.",
        route=(0.53, 0.62),
        default_duration_s=60.0,
        disturbances=(
            Disturbance("cellular", 0.35, 0.60, outage=True,
                        label="Test event: private 5G outage"),
        ),
    ),
}

DEFAULT_SCENARIO = "gradual_coverage"


def get(scenario_id: str) -> Scenario:
    if scenario_id not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario_id!r}")
    return SCENARIOS[scenario_id]


class FaultInjector:
    """Fires the scenario's faults at the exact moment they are defined for.

    Servers ask it before preparing a session. It is deterministic: the first
    `count` preparations (optionally of one named server) are refused.
    """

    def __init__(self, faults: Tuple[Fault, ...] = (), telemetry=None) -> None:
        self.remaining: List[List[Any]] = [[f, f.count] for f in faults]
        self.telemetry = telemetry
        self.fired: List[Dict[str, Any]] = []

    def check_prepare(self, server_id: str) -> Optional[str]:
        for slot in self.remaining:
            fault, left = slot
            if fault.type != "target_prepare_failure" or left <= 0:
                continue
            if fault.server not in ("any", server_id):
                continue
            slot[1] -= 1
            record = {"fault": fault.type, "label": fault.label, "server": server_id,
                      "remaining": slot[1]}
            self.fired.append(record)
            if self.telemetry is not None:
                self.telemetry.emit("fault_injected", "fault_injector", **record)
            return "prepare_refused (injected test fault)"
        return None


def describe_all() -> List[Dict[str, Any]]:
    return [copy.deepcopy(s.as_dict()) for s in SCENARIOS.values()]
