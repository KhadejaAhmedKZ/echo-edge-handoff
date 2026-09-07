"""Tests for the parts where a silent mistake would invalidate the results."""
from __future__ import annotations

import asyncio
import json
import math

import pytest

from echo_sim import protocol as P
from echo_sim.agents.decision import DecisionEngine
from echo_sim.agents.intent import IntentAgent
from echo_sim.agents.predictor import Predictor
from echo_sim.agents.watcher import Watcher
from echo_sim.config import EDGES, NETWORKS, RunConfig, backhaul_ms
from echo_sim.edge import EdgeService
from echo_sim.metrics import FrameRecord, RunMetrics, percentile
from echo_sim.telemetry import json_safe
from echo_sim.world import World


# -- world -----------------------------------------------------------------

def test_coverage_degrades_smoothly_rather_than_flipping():
    """The Predictor is only meaningful if quality ramps instead of stepping."""
    w = World(duration_s=100.0)
    qs = []
    for pos in [i / 60 for i in range(61)]:
        w.freeze(pos)
        qs.append(w.quality("wifi"))
    biggest_step = max(abs(b - a) for a, b in zip(qs, qs[1:]))
    assert biggest_step < 0.35, "wifi coverage should ramp, not cliff"
    assert qs[0] > 0.7 and qs[-1] == 0.0


def test_wired_only_exists_at_the_dock():
    w = World(duration_s=100.0)
    w.freeze(0.2)
    assert not w.profile("wired").available
    w.freeze(0.98)
    assert w.profile("wired").available


def test_satellite_is_always_available_and_always_slow():
    w = World(duration_s=100.0)
    for pos in (0.1, 0.5, 0.9):
        w.freeze(pos)
        p = w.profile("satellite")
        assert p.available
        assert p.rtt_ms > 400, "satellite latency is physics, not congestion"


def test_unavailable_network_serialises_without_infinity():
    w = World(duration_s=100.0)
    w.freeze(0.2)
    d = w.profile("wired").as_dict()
    json.dumps(d)          # would raise on inf under allow_nan=False
    assert d["rtt_ms"] is None
    assert d["available"] is False


def test_json_safe_strips_non_finite_values():
    out = json_safe({"a": math.inf, "b": [1.0, math.nan], "c": {"d": -math.inf}})
    assert out == {"a": None, "b": [1.0, None], "c": {"d": None}}
    json.dumps(out, allow_nan=False)


# -- backhaul --------------------------------------------------------------

def test_reaching_a_far_edge_costs_extra():
    assert backhaul_ms("wifi", "A") == 0.0
    assert backhaul_ms("cellular", "A") > 0.0
    assert backhaul_ms("satellite", "A") > backhaul_ms("cellular", "A")


# -- session state ---------------------------------------------------------

def test_session_state_round_trips_and_versions():
    st = P.SessionState(session_id="robot-01", model_id="m", model_version="1.0")
    st.bump(42, {"tracks": [{"id": 1}]})
    st.bump(43, {"tracks": [{"id": 1}, {"id": 2}]})
    assert st.state_version == 2
    assert st.last_processed_frame == 43
    clone = P.SessionState.from_dict(json.loads(json.dumps(st.to_dict())))
    assert clone.state_version == st.state_version
    assert clone.tracking_state == st.tracking_state


def test_state_is_small_enough_to_move_cheaply():
    st = P.SessionState(session_id="robot-01", model_id="m", model_version="1.0")
    st.bump(9000, {"tracks": [{"id": i, "cls": "valve", "x": .5, "y": .5}
                              for i in range(8)]})
    assert st.size_bytes() < 4096, "a handoff must not turn into a model download"


def test_frame_decoder_handles_split_and_merged_writes():
    msgs = [P.message(P.ECHO_FRAME, frame_no=i) for i in range(3)]
    blob = b"".join(P.encode(m) for m in msgs)
    dec = P.FrameDecoder()
    got = []
    for i in range(0, len(blob), 7):        # arbitrary chunk boundaries
        got.extend(dec.feed(blob[i:i + 7]))
    assert [m["frame_no"] for m in got] == [0, 1, 2]


# -- edge service ----------------------------------------------------------

@pytest.mark.asyncio
async def test_transferred_state_avoids_the_cold_start():
    cfg = RunConfig()
    cold = EdgeService(EDGES["B"], cfg)
    warm = EdgeService(EDGES["B"], cfg)
    sid = "robot-01"

    await cold.handle(P.message(P.ECHO_INIT, session_id=sid))
    assert cold.sessions[sid].warmup_left == cfg.warmup_frames

    st = P.SessionState(session_id=sid, model_id="m", model_version="1.0")
    st.bump(500, {"tracks": []})
    await warm.handle(P.message(P.ECHO_PREPARE, session_id=sid))
    ready = await warm.handle(P.message(P.ECHO_STATE, session_id=sid,
                                        state=st.to_dict()))
    assert ready["type"] == P.ECHO_READY
    assert warm.sessions[sid].warmup_left == 0
    assert warm.sessions[sid].state.last_processed_frame == 500


@pytest.mark.asyncio
async def test_a_prepared_edge_refuses_frames_until_it_is_committed():
    svc = EdgeService(EDGES["C"], RunConfig())
    sid = "robot-01"
    await svc.handle(P.message(P.ECHO_PREPARE, session_id=sid))
    err = await svc.handle(P.message(P.ECHO_FRAME, session_id=sid, frame_no=1))
    assert err["type"] == P.ECHO_ERROR and err["reason"] == "not_active"

    await svc.handle(P.message(P.ECHO_COMMIT, session_id=sid))
    ok = await svc.handle(P.message(P.ECHO_FRAME, session_id=sid, frame_no=1))
    assert ok["type"] == P.ECHO_RESULT


@pytest.mark.asyncio
async def test_replayed_frames_are_marked_duplicate_not_reprocessed():
    svc = EdgeService(EDGES["A"], RunConfig())
    sid = "robot-01"
    await svc.handle(P.message(P.ECHO_INIT, session_id=sid))
    await svc.handle(P.message(P.ECHO_FRAME, session_id=sid, frame_no=10))
    again = await svc.handle(P.message(P.ECHO_FRAME, session_id=sid, frame_no=10))
    assert again["duplicate"] is True
    assert svc.sessions[sid].frames_served == 1


# -- decision --------------------------------------------------------------

class _StubEdge:
    def __init__(self, edge_id, infer_ms, cpu=0.2):
        self.edge_id, self.infer_ms, self.cpu = edge_id, infer_ms, cpu

    def health(self):
        return {"edge_id": self.edge_id, "available": True, "cpu": self.cpu,
                "active_sessions": 1, "expected_inference_ms": self.infer_ms,
                "label": self.edge_id, "home_network": "wifi", "frames_served": 0,
                "prepared_sessions": 0}


def _engine(position: float):
    world = World(duration_s=100.0)
    world.freeze(position)
    services = {eid: _StubEdge(eid, EDGES[eid].base_inference_ms)
                for eid in EDGES}
    watcher = Watcher(world, services)
    for _ in range(12):
        watcher.tick(0.0)
    predictor = Predictor(watcher, horizon_s=2.0)
    return DecisionEngine(watcher, predictor, IntentAgent("inference"),
                          lambda: world.position), watcher


def test_satellite_is_rejected_even_though_it_is_available():
    """Availability is not suitability - the core claim of the project."""
    engine, watcher = _engine(0.70)
    assert watcher.latest_networks["satellite"].available
    best = engine.best()
    assert best.network_id != "satellite"


def test_hysteresis_blocks_a_marginal_switch():
    engine, _ = _engine(0.05)
    best = engine.best()
    current, challenger, reason = engine.evaluate(best.network_id, best.edge_id)
    assert challenger is None
    assert "best pair" in reason


def test_a_dead_path_switches_immediately_without_waiting_for_patience():
    engine, _ = _engine(0.95)          # wifi is long gone at the dock
    current, challenger, reason = engine.evaluate("wifi", "A")
    assert challenger is not None
    assert "unreachable" in reason


# -- metrics ---------------------------------------------------------------

def test_percentile_matches_hand_computed_values():
    xs = [10, 20, 30, 40, 50]
    assert percentile(xs, 0.0) == 10
    assert percentile(xs, 0.5) == 30
    assert percentile(xs, 1.0) == 50
    assert percentile([], 0.5) != percentile([], 0.5)   # nan


def test_inference_latency_gap_is_crossing_minus_settled():
    m = RunMetrics(mode="echo")
    m.crossing_windows = [(10.0, 12.0)]
    for t in [1.0, 2.0, 3.0, 4.0]:
        m.add(FrameRecord(1, t, t, 100.0, 20.0, "A", "wifi"))
    for t in [10.5, 11.0, 11.5]:
        m.add(FrameRecord(1, t, t, 300.0, 20.0, "A", "wifi"))
    s = m.summary()
    assert s["p95_settled_ms"] == 100.0
    assert s["p95_crossing_ms"] == 300.0
    assert s["zone_gap_ms"] == 200.0


def test_longest_gap_finds_the_visible_freeze():
    m = RunMetrics(mode="tcp")
    for sent, recv in [(0.0, 0.0), (0.1, 0.1), (0.2, 1.4), (1.5, 1.5)]:
        m.add(FrameRecord(1, sent, recv, 50.0, 10.0, "A", "wifi"))
    assert round(m.longest_gap_ms()) == 1300


# -- orchestrator ----------------------------------------------------------

class _StubHandoff:
    def __init__(self, state="STABLE"):
        self.state = state


def _orchestrator(position: float, handoff_state="STABLE"):
    from echo_sim.agents.handoff import STABLE  # noqa: F401
    from echo_sim.agents.orchestrator import Orchestrator
    from echo_sim.agents.recovery import RecoveryAgent

    engine, watcher = _engine(position)
    handoff = _StubHandoff(handoff_state)
    recovery = RecoveryAgent(watcher, engine)
    orch = Orchestrator(engine, handoff, recovery, IntentAgent("inference"),
                        cooldown_s=3.0)
    return orch, engine, recovery


def test_orchestrator_refuses_a_second_move_while_one_is_in_flight():
    from echo_sim.agents.orchestrator import HOLD
    orch, _, _ = _orchestrator(0.95, handoff_state="TRANSFERRING")
    action, _ = orch.plan("wifi", "A")          # wifi is dead at the dock
    assert action.kind == HOLD
    assert "already in flight" in action.reason


def test_orchestrator_respects_recovery_backoff():
    from echo_sim.agents.orchestrator import HOLD
    orch, engine, recovery = _orchestrator(0.95)
    best = engine.best()
    recovery.block(best.edge_id, 30.0)
    action, _ = orch.plan("wifi", "A")
    assert action.kind == HOLD
    assert action.veto.startswith("blocked:")


def test_orchestrator_cooldown_does_not_strand_a_dead_path():
    """Cooling down is a preference; being unreachable is not."""
    from echo_sim.agents.orchestrator import HANDOFF
    orch, _, _ = _orchestrator(0.95)
    orch.note_move()                            # start the cooldown
    assert orch.cooldown_left_s > 0
    action, current = orch.plan("wifi", "A")    # wifi has no coverage here
    assert not current.reachable
    assert action.kind == HANDOFF


def test_orchestrator_prefers_a_path_migration_over_moving_compute():
    from echo_sim.agents.orchestrator import MIGRATE_PATH
    from echo_sim.agents.decision import Candidate
    orch, engine, _ = _orchestrator(0.05)

    best = engine.best()
    # Force a challenger on the same edge but a different network.
    other = "cellular" if best.network_id != "cellular" else "wifi"
    engine.evaluate = lambda n, e: (                      # type: ignore[assignment]
        engine.current_candidate(n, e),
        Candidate(best.edge_id, other, 1.0, 10, 1, 0.0, 10, .2, 20),
        "forced",
    )
    action, _ = orch.plan(best.network_id, best.edge_id)
    assert action.kind == MIGRATE_PATH
    assert action.target.edge_id == best.edge_id
