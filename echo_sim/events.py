"""The one telemetry contract.

Every consumer - the live dashboard, recorded replay, the per-run evidence
files and the result generator - reads events in exactly this shape. Keeping a
single schema is what stops the visual demo from drifting away from the
evidence: if the dashboard shows it, it is in the recording, and if it is in
the recording, it was emitted by the component named in `source_component`.

Envelope (every event):

    run_id            str   unique run identifier
    sequence_number   int   strictly increasing within a run, from 1
    elapsed_time      float seconds since the run's telemetry started
    event_type        str   one of EVENT_TYPES
    controller_mode   str   one of CONTROLLER_MODES (or "" before a run starts)
    scenario_id       str   scenario identifier
    source_component  str   one of COMPONENTS
    payload           dict  event-specific fields, JSON-safe (no inf / NaN)
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

SCHEMA_VERSION = "echo-events/1"

ENVELOPE_FIELDS = (
    "run_id", "sequence_number", "elapsed_time", "event_type",
    "controller_mode", "scenario_id", "source_component", "payload",
)

# Controller modes, all running in the same engine against the same scenario.
TCP_RECONNECT = "tcp_reconnect"
QUIC_FIXED = "quic_fixed"
MEASURED = "measured"
PREDICTIVE = "predictive"
CONTROLLER_MODES = (TCP_RECONNECT, QUIC_FIXED, MEASURED, PREDICTIVE)

# Older recordings and scripts used these names.
LEGACY_MODE_ALIASES = {"tcp": TCP_RECONNECT, "quic": QUIC_FIXED,
                       "echo": PREDICTIVE, "reactive": MEASURED}


def canonical_mode(mode: str) -> str:
    mode = LEGACY_MODE_ALIASES.get(mode, mode)
    if mode not in CONTROLLER_MODES:
        raise ValueError(f"unknown controller mode: {mode!r}")
    return mode


COMPONENTS = (
    "runner", "world", "client", "watcher", "decision", "orchestrator",
    "handoff", "recovery", "troubleshooter", "explainer", "transport",
    "edge", "metrics", "fault_injector", "importer",
)

# event_type -> required payload keys. Extra keys are allowed.
EVENT_TYPES: Dict[str, tuple] = {
    # lifecycle
    "mission_started": ("scenario_id", "controller_mode", "seed", "duration_s"),
    "session_started": ("server", "network"),
    "mission_completed": ("summary",),
    "run_error": ("reason",),
    # what is changing
    "network_observed": ("position", "zone", "networks", "servers",
                         "current_server", "current_network"),
    "zone_changed": ("from_zone", "to_zone", "position"),
    "fault_injected": ("fault", "label"),
    # what ECHO is deciding
    "candidate_scored": ("candidates",),
    "decision_made": ("action", "reason"),
    "explanation": ("text",),
    "diagnosis": ("cause", "headline"),
    # handover
    "handoff_phase_changed": ("phase",),
    "handoff_completed": ("source_server", "target_server", "total_ms"),
    "handoff_failed": ("source_server", "target_server", "reason"),
    "recovery_planned": ("action", "reason"),
    "path_migrated": ("from_network", "to_network", "server"),
    "connection_lost": ("server",),
    "reconnected": ("server", "network", "outage_ms"),
    "state_verified": ("ok", "property", "session_id"),
    # application experience
    "request_completed": ("request_no", "sent_t", "response_ms", "server", "network"),
    "request_lost": ("request_no", "sent_t", "reason"),
    "metrics_updated": ("summary",),
    "playback_buffer": ("buffer_s", "stalls"),
    # servers
    "server_event": ("event", "server", "session_id"),
    # diagnostics
    "agent_error": ("error",),
}

# Event types written to the per-run evidence split files.
FRAME_EVENTS = ("request_completed", "request_lost")
DECISION_EVENTS = ("decision_made",)
HANDOFF_EVENTS = ("handoff_phase_changed", "handoff_completed", "handoff_failed",
                  "recovery_planned", "path_migrated", "connection_lost",
                  "reconnected", "state_verified")


class SchemaError(ValueError):
    pass


def _finite(value: Any, path: str) -> None:
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        raise SchemaError(f"non-finite number at {path}")
    if isinstance(value, dict):
        for k, v in value.items():
            _finite(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _finite(v, f"{path}[{i}]")


def validate(evt: Dict[str, Any]) -> Dict[str, Any]:
    """Raise SchemaError if `evt` breaks the contract; return it otherwise."""
    missing = [f for f in ENVELOPE_FIELDS if f not in evt]
    if missing:
        raise SchemaError(f"missing envelope fields: {missing}")
    et = evt["event_type"]
    if et not in EVENT_TYPES:
        raise SchemaError(f"unknown event_type {et!r}")
    if evt["source_component"] not in COMPONENTS:
        raise SchemaError(f"unknown source_component {evt['source_component']!r}")
    mode = evt["controller_mode"]
    if mode and mode not in CONTROLLER_MODES:
        raise SchemaError(f"unknown controller_mode {mode!r}")
    if not isinstance(evt["sequence_number"], int) or evt["sequence_number"] < 1:
        raise SchemaError("sequence_number must be a positive int")
    if not isinstance(evt["elapsed_time"], (int, float)):
        raise SchemaError("elapsed_time must be a number")
    payload = evt["payload"]
    if not isinstance(payload, dict):
        raise SchemaError("payload must be an object")
    need = [k for k in EVENT_TYPES[et] if k not in payload]
    if need:
        raise SchemaError(f"{et} payload missing {need}")
    _finite(payload, "payload")
    return evt


def validate_stream(events: List[Dict[str, Any]]) -> int:
    """Validate a whole recording, including sequence ordering. Returns count."""
    last = 0
    for evt in events:
        validate(evt)
        if evt["sequence_number"] <= last:
            raise SchemaError(
                f"sequence_number not increasing at {evt['sequence_number']}")
        last = evt["sequence_number"]
    return len(events)
