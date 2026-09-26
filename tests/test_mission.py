"""Tests for the Inspection Mission additions: telemetry contract, scenarios,
faults, continuity checks, experience metrics, evidence store and API."""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from echo_sim import events as E
from echo_sim import protocol as P
from echo_sim import scenarios as S
from echo_sim.agents.intent import (CONSERVATIVE_INTENT, UNKNOWN, IntentAgent,
                                    classify_traffic)
from echo_sim.config import EDGES, RunConfig
from echo_sim.edge import EdgeService
from echo_sim.metrics import FrameRecord, RunMetrics
from echo_sim.telemetry import Telemetry
from echo_sim.world import World


# -- activity classification (the unreachable-branch bug) ---------------------

def test_realtime_call_is_reachable_now():
    """Previously the broad inference rule matched first, so this was impossible."""
    assert classify_traffic(50, 1_200) == "realtime_call"


@pytest.mark.parametrize("rate,size,expected", [
    (20, 24_000, "inference"),
    (8, 4_000, "inference"),
    (1, 2_000_000, "bulk_transfer"),
    (1, 400_000, "streaming"),
    (5, 1_000_000, "streaming"),
    (0, 24_000, UNKNOWN),
    (3, 1_000, UNKNOWN),          # slow and tiny: not a pattern we know
    (100, 300_000, UNKNOWN),      # fast and large: not a pattern we know
])
def test_traffic_rules_are_disjoint_and_cover_representative_inputs(rate, size, expected):
    assert classify_traffic(rate, size) == expected


def test_unknown_traffic_falls_back_to_the_conservative_live_policy():
    agent = IntentAgent()
    agent.observe(3, 1_000)
    assert agent.observed == UNKNOWN
    assert agent.intent == CONSERVATIVE_INTENT
    assert agent.is_live


def test_explicit_profile_is_never_overridden_by_the_classifier():
    agent = IntentAgent(profile="buffered_playback")
    for _ in range(10):
        agent.observe(20, 24_000)          # looks exactly like live inference
    assert agent.observed == "inference"
    assert agent.intent == "streaming"     # still the selected profile


# -- telemetry contract ---------------------------------------------------------

def _evt(**over):
    base = {"run_id": "r", "sequence_number": 1, "elapsed_time": 0.1,
            "event_type": "request_lost", "controller_mode": "measured",
            "scenario_id": "gradual_coverage", "source_component": "client",
            "payload": {"request_no": 1, "sent_t": 0.0, "reason": "timeout"}}
    base.update(over)
    return base


def test_valid_event_passes():
    E.validate(_evt())


@pytest.mark.parametrize("bad", [
    {"event_type": "made_up"},
    {"source_component": "nobody"},
    {"controller_mode": "echo"},
    {"sequence_number": 0},
    {"payload": {"request_no": 1}},
    {"payload": {"request_no": 1, "sent_t": float("inf"), "reason": "x"}},
])
def test_contract_violations_are_rejected(bad):
    with pytest.raises(E.SchemaError):
        E.validate(_evt(**bad))


def test_stream_requires_increasing_sequence():
    with pytest.raises(E.SchemaError):
        E.validate_stream([_evt(sequence_number=2), _evt(sequence_number=2)])


def test_telemetry_emits_the_envelope_and_numbers_events(tmp_path):
    path = tmp_path / "e.jsonl"
    tel = Telemetry(str(path), run_id="r1", controller_mode="measured",
                    scenario_id="gradual_coverage", strict=True)
    tel.emit("explanation", "explainer", text="hello")
    tel.emit("request_lost", "client", request_no=1, sent_t=0.0, reason="timeout")
    tel.close()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["sequence_number"] for r in rows] == [1, 2]
    assert set(E.ENVELOPE_FIELDS) <= set(rows[0])
    E.validate_stream(rows)


def test_non_strict_telemetry_records_a_violation_instead_of_dropping_it():
    tel = Telemetry(controller_mode="measured")
    evt = tel.emit("request_lost", "client", request_no=1)   # missing keys
    assert evt["event_type"] == "agent_error"
    assert tel.schema_errors == 1


def test_legacy_mode_names_map_to_neutral_modes():
    assert E.canonical_mode("echo") == E.PREDICTIVE
    assert E.canonical_mode("tcp") == E.TCP_RECONNECT
    with pytest.raises(ValueError):
        E.canonical_mode("intelligent")


# -- scenarios and world -------------------------------------------------------

def test_route_maps_mission_time_onto_part_of_the_site():
    w = World(60.0, route=(0.5, 0.6))
    w.freeze_time(0.5)
    assert abs(w.position - 0.55) < 1e-9


def test_disturbance_adds_delay_only_inside_its_window():
    scen = S.get("brief_disturbance")
    w = World(60.0, route=scen.route, disturbances=scen.disturbances)
    w.freeze_time(0.30)
    before = w.profile("wifi").rtt_ms
    w.freeze_time(0.48)
    during = w.profile("wifi").rtt_ms
    assert during - before > 100


def test_outage_makes_a_network_unusable():
    scen = S.get("backup_only")
    w = World(60.0, route=scen.route, disturbances=scen.disturbances)
    w.freeze_time(0.40)
    assert not w.profile("cellular").available
    assert w.profile("satellite").available        # the backup path remains
    w.freeze_time(0.90)
    assert w.profile("cellular").available


def test_every_scenario_is_versioned_and_described():
    for scen in S.SCENARIOS.values():
        assert scen.version >= 1 and scen.title and scen.tests
        for d in scen.disturbances:
            assert d.label.startswith("Test event:")
        for f in scen.faults:
            assert f.label.startswith("Test event:")


def test_fault_injector_fires_exactly_count_times_and_says_so():
    seen = []

    class Tel:
        def emit(self, et, src, **p):
            seen.append((et, src, p))

    inj = S.FaultInjector((S.Fault("target_prepare_failure", count=1,
                                   label="Test event: x"),), Tel())
    assert inj.check_prepare("B") is not None
    assert inj.check_prepare("B") is None
    assert seen[0][0] == "fault_injected" and seen[0][2]["server"] == "B"


@pytest.mark.asyncio
async def test_injected_prepare_failure_is_refused_by_the_server():
    svc = EdgeService(EDGES["B"], RunConfig())
    svc.faults = S.FaultInjector((S.Fault("target_prepare_failure", count=1),))
    r1 = await svc.handle(P.message(P.ECHO_PREPARE, session_id="s"))
    r2 = await svc.handle(P.message(P.ECHO_PREPARE, session_id="s"))
    assert r1["type"] == P.ECHO_ERROR and "injected" in r1["reason"]
    assert r2["type"] == P.ECHO_ACK


@pytest.mark.asyncio
async def test_draining_server_finishes_outstanding_work_only():
    svc = EdgeService(EDGES["A"], RunConfig())
    await svc.handle(P.message(P.ECHO_INIT, session_id="s"))
    await svc.handle(P.message(P.ECHO_DRAIN, session_id="s", finish_up_to=10))
    ok = await svc.handle(P.message(P.ECHO_FRAME, session_id="s", frame_no=10))
    no = await svc.handle(P.message(P.ECHO_FRAME, session_id="s", frame_no=11))
    assert ok["type"] == P.ECHO_RESULT
    assert no["type"] == P.ECHO_ERROR


# -- decision fixes ------------------------------------------------------------

def _engine_at(position):
    from echo_sim.agents.decision import DecisionEngine
    from echo_sim.agents.predictor import Predictor
    from echo_sim.agents.watcher import Watcher

    class Srv:
        def __init__(self, eid, sessions):
            self.eid, self.sessions = eid, sessions

        def health(self):
            e = EDGES[self.eid]
            return {"edge_id": self.eid, "available": True,
                    "cpu": 0.18 + 0.22 * self.sessions,
                    "active_sessions": self.sessions,
                    "expected_inference_ms": e.base_inference_ms + e.load_penalty_ms * self.sessions,
                    "session_cpu_cost": 0.22, "load_penalty_ms": e.load_penalty_ms}

    w = World(100.0)
    w.freeze(position)
    return w, Srv, Watcher, Predictor, DecisionEngine


def test_own_session_does_not_make_the_current_server_look_worse():
    """The ping-pong fix: every server is scored as if it hosted this session."""
    w, Srv, Watcher, Predictor, DecisionEngine = _engine_at(0.05)
    scores = {}
    for host in ("A", "B"):
        services = {eid: Srv(eid, 1 if eid == host else 0) for eid in EDGES}
        watcher = Watcher(w, services)
        for _ in range(12):
            watcher.tick(0.0)
        eng = DecisionEngine(watcher, Predictor(watcher, 0.0), IntentAgent(),
                             lambda: w.position)
        a = eng.score_pair("wifi", "A", eng.intent.weights, hosting=host)
        b = eng.score_pair("wifi", "B", eng.intent.weights, hosting=host)
        scores[host] = (a.inference_ms, b.inference_ms, a.cpu, b.cpu)
    # Same world, session on A or on B: the comparison between A and B is unchanged.
    assert scores["A"] == pytest.approx(scores["B"], abs=0.05)


def test_no_discretionary_switch_before_enough_measurements():
    w, Srv, Watcher, Predictor, DecisionEngine = _engine_at(0.05)
    watcher = Watcher(w, {eid: Srv(eid, 0) for eid in EDGES})
    watcher.tick(0.0)
    eng = DecisionEngine(watcher, Predictor(watcher, 0.0), IntentAgent(),
                         lambda: w.position)
    eng.best = lambda hosting=None: eng.score_pair("wifi", "D", eng.intent.weights)
    _, challenger, reason = eng.evaluate("cellular", "B")
    assert challenger is None and "learning" in reason


def test_returning_to_a_server_just_left_needs_stronger_evidence():
    w, Srv, Watcher, Predictor, DecisionEngine = _engine_at(0.05)
    watcher = Watcher(w, {eid: Srv(eid, 0) for eid in EDGES})
    for _ in range(12):
        watcher.tick(0.0)
    eng = DecisionEngine(watcher, Predictor(watcher, 0.0), IntentAgent(),
                         lambda: w.position, margin=0.01, patience=1)
    best = eng.best(hosting="B")
    assert best.edge_id != "B"
    eng.note_left(best.edge_id)
    _, challenger, reason = eng.evaluate("cellular", "B")
    # patience 1 would normally switch at once; returning needs 3 checks
    assert challenger is None and "just left" in reason


# -- continuity check before switching ------------------------------------------

class _FakeTransport:
    def __init__(self, ready_version_offset=0):
        self.off = ready_version_offset

    async def open_standby(self, *a):
        return True

    async def request_standby(self, msg, timeout):
        t = msg["type"]
        if t == P.ECHO_PREPARE:
            return P.message(P.ECHO_ACK, session_id=msg["session_id"])
        if t == P.ECHO_STATE:
            st = msg["state"]
            return P.message(P.ECHO_READY, session_id=st["session_id"],
                             state_version=st["state_version"] + self.off,
                             last_processed_frame=st["last_processed_frame"])
        if t == P.ECHO_VERIFY:
            return P.message(P.ECHO_RESULT, session_id=msg["session_id"])
        if t == P.ECHO_COMMIT:
            return P.message(P.ECHO_ACK, session_id=msg["session_id"])

    async def promote_standby(self):
        pass

    async def close_standby(self):
        pass

    async def drain(self, *a):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("offset,ok", [(0, True), (-3, False)])
async def test_switch_is_refused_when_the_target_state_does_not_match(offset, ok):
    from echo_sim.agents.decision import Candidate
    from echo_sim.agents.handoff import HandoffManager
    events = []

    class Tel:
        def emit(self, et, src, **p):
            events.append((et, p))

    hm = HandoffManager(_FakeTransport(offset), RunConfig(), Tel())
    st = P.SessionState(session_id="robot-01", model_id="m", model_version="1")
    st.bump(1500, {"tracks": []})
    res = await hm.execute("A", Candidate("B", "cellular", 1, 1, 1, 0, 1, 0, 1), st)
    assert res.ok is ok
    checks = [p for et, p in events if et == "state_verified"]
    assert checks and checks[0]["ok"] is ok
    if not ok:
        assert res.failed_phase == "transfer"


# -- experience metrics -------------------------------------------------------

def test_late_results_count_lost_and_slow_against_each_threshold():
    m = RunMetrics(mode="measured", deadline_ms=150.0)
    for i, ms in enumerate([50, 120, 160, 250]):
        m.add(FrameRecord(i, i, i, float(ms), 1.0, "A", "wifi"))
    m.add(FrameRecord(9, 9, None, None, None, "A", "wifi", lost=True))
    s = m.summary()
    assert s["late_by_threshold"]["100"]["late"] == 4     # 120,160,250 + lost
    assert s["late_by_threshold"]["150"]["late"] == 3
    assert s["late_by_threshold"]["200"]["late"] == 2
    assert s["late_results_pct"] == 60.0
    assert s["results_lost"] == 1


def test_unnecessary_reversal_is_a_quick_return_to_the_server_just_left():
    m = RunMetrics(mode="measured", reversal_window_s=10.0)
    m.note_server_switch(1.0, "A", "B", "handoff")
    m.note_server_switch(5.0, "B", "A", "handoff")      # back within 10 s
    m.note_server_switch(30.0, "A", "B", "handoff")     # long after
    m.note_server_switch(50.0, "B", "D", "handoff")
    assert m.unnecessary_reversals() == 1


# -- evidence store ------------------------------------------------------------

def test_evidence_run_directory_manifest_and_verification(tmp_path, monkeypatch):
    from echo_sim import evidence as EV
    from echo_sim.runner import run_configuration
    monkeypatch.setattr(EV, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(EV, "COMPARISONS", str(tmp_path / "comparisons"))
    cfg = RunConfig(mode="measured", duration_s=20)
    rec = EV.RunRecorder(cfg)
    rec.write_configuration(run_configuration(cfg))
    tel = Telemetry(rec.events_path, run_id=rec.run_id, controller_mode="measured",
                    scenario_id="gradual_coverage", strict=True)
    tel.emit("request_completed", "client", request_no=1, sent_t=0.0,
             response_ms=40.0, server="A", network="wifi")
    tel.emit("decision_made", "orchestrator", action="hold", reason="fine")
    tel.emit("state_verified", "handoff", ok=True, property="x", session_id="s")
    tel.close()
    man = rec.finalize({"p95_ms": 40.0}, "completed")
    assert man["schema_valid"] and man["data_origin"] == "live"
    assert man["code_version"]["source_sha256"]
    assert set(man["files"]) >= {"events.jsonl", "frames.jsonl", "summary.json"}
    assert EV.verify_run(rec.run_id)["ok"]
    with open(os.path.join(rec.dir, "summary.json"), "a") as fh:
        fh.write(" ")
    assert not EV.verify_run(rec.run_id)["ok"]
    with pytest.raises(FileExistsError):
        EV.RunRecorder(cfg, run_id=rec.run_id)       # never overwrite a run
    for bad in ("../x", "", ".hidden", "a/b"):
        with pytest.raises(FileNotFoundError):
            EV.run_dir(bad)


# -- API -------------------------------------------------------------------------

def test_api_rejects_out_of_bounds_configuration():
    from fastapi.testclient import TestClient
    from dashboard.server import app
    c = TestClient(app)
    assert c.post("/api/runs", json={"duration_s": 5}).status_code == 422
    assert c.post("/api/runs", json={"controller_mode": "intelligent"}).status_code == 422
    assert c.post("/api/runs", json={"scenario_id": "nope"}).status_code == 422
    assert c.get("/api/runs/..%2F..%2Fetc").status_code == 404


def test_api_describes_capabilities_and_scenarios():
    from fastapi.testclient import TestClient
    from dashboard.server import app
    c = TestClient(app)
    caps = c.get("/api/capabilities").json()
    assert {m["id"] for m in caps["controller_modes"]} == set(E.CONTROLLER_MODES)
    assert caps["default_mode"] == E.MEASURED
    assert any(s["status"] == "proposed" for s in caps["status"])
    sc = c.get("/api/scenarios").json()
    assert {s["id"] for s in sc["scenarios"]} == set(S.SCENARIOS)

# Stability guards must reject interrupted evidence, not just count ticks.
def test_alternating_challengers_do_not_accumulate_votes(monkeypatch):
    from echo_sim.agents.decision import Candidate
    w, Srv, Watcher, Predictor, Engine = _engine_at(.70)
    watcher = Watcher(w, {eid: Srv(eid, 0) for eid in EDGES})
    for i in range(12): watcher.tick(i * .25)
    eng = Engine(watcher, Predictor(watcher, 0), IntentAgent(), lambda: .70, patience=3, stable_link_hold_s=0)
    current = Candidate('A', 'cellular', 200, 100, 0, 0, 20, .2, 120)
    eng.current_candidate = lambda n,e: current
    choices = [Candidate(e, 'cellular', 50, 30, 0, 0, 20, .2, 50) for e in ['B','D']]
    for i in range(10):
        eng.best = lambda hosting=None, i=i: choices[i % 2]
        assert eng.evaluate('cellular','A')[1] is None


def test_strong_coverage_requires_sustained_improvement(monkeypatch):
    from echo_sim.agents.decision import Candidate
    w, Srv, Watcher, Predictor, Engine = _engine_at(.05)
    watcher = Watcher(w, {eid: Srv(eid, 0) for eid in EDGES})
    for i in range(12): watcher.tick(i * .25)
    eng = Engine(watcher, Predictor(watcher, 0), IntentAgent(), lambda: .05, patience=1, stable_link_hold_s=5)
    # Same good Wi-Fi, but A has a temporary processing spike.
    watcher.latest_edges['A']['expected_inference_ms'] = 250
    eng.current_candidate = lambda n,e: Candidate('A','wifi',300,30,0,0,250,.2,280)
    eng.best = lambda hosting=None: Candidate('B','wifi',100,70,0,0,30,.2,100)
    clock = [100.0]
    monkeypatch.setattr('echo_sim.agents.decision.time.monotonic', lambda: clock[0])
    assert eng.evaluate('wifi','A')[1] is None
    clock[0] += 4
    assert eng.evaluate('wifi','A')[1] is None
    clock[0] += 2
    assert eng.evaluate('wifi','A')[1] is not None


def test_recovered_link_discards_stale_forecast(monkeypatch):
    from echo_sim.agents.decision import Candidate
    w, Srv, Watcher, Predictor, Engine = _engine_at(.05)
    watcher = Watcher(w, {eid: Srv(eid, 0) for eid in EDGES})
    for i in range(12): watcher.tick(i * .25)
    eng = Engine(watcher, Predictor(watcher, 0), IntentAgent(), lambda: .05, patience=1, stable_link_hold_s=5)
    eng.current_candidate = lambda n,e: Candidate('A','wifi',300,250,0,0,30,.2,280)
    eng.best = lambda hosting=None: Candidate('B','cellular',100,70,0,0,30,.2,100)
    assert eng.evaluate('wifi','A')[1] is None
    assert not eng._streak


def test_visual_assets_are_local_and_served():
    from fastapi.testclient import TestClient
    from dashboard.server import app
    c = TestClient(app)
    for path in ['/assets/mission.js','/assets/mission.css','/assets/earth.jpg','/assets/three.module.min.js','/assets/three.core.min.js']:
        assert c.get(path).status_code == 200
    assert 'robotCanvas' in c.get('/').text


def test_model_proxy_validates_sample_and_forwards_response(monkeypatch):
    from fastapi.testclient import TestClient
    from dashboard import server
    calls=[]
    def fake(path,timeout=8):
        calls.append(path)
        return {'model':'Random Forest v2','mode':'recorded-data','probability':.7}
    monkeypatch.setattr(server,'fetch_json',fake)
    c=TestClient(server.app)
    assert c.get('/api/model/example?sample=-1').status_code==422
    result=c.get('/api/model/example?sample=3')
    assert result.status_code==200
    assert result.json()['mode']=='recorded-data'
    assert calls==['/api/ml/status?sample=3']


def test_orbit_proxy_preserves_source_and_stale_flag(monkeypatch):
    from fastapi.testclient import TestClient
    from dashboard import server
    monkeypatch.setattr(server,'fetch_json',lambda *args: {'source':'test orbit source','catalogStale':True})
    c=TestClient(server.app)
    assert c.get('/api/orbit/status').json()['catalogStale'] is True
    assert c.get('/api/orbit/constellation').json()['source']=='test orbit source'
