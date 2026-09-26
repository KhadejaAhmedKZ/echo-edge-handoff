"""Canonical evidence store.

    experiments/
      configurations/             scenario + controller definitions, versioned
      runs/<run-id>/
        configuration.json        everything needed to reproduce the run
        events.jsonl              the complete telemetry stream (replay source)
        frames.jsonl              request_completed / request_lost events
        decisions.jsonl           decision_made events
        handoffs.jsonl            handover phases, results, continuity checks
        summary.json              metrics from the one shared implementation
        manifest.json             identity, provenance, file hashes
      comparisons/<id>.json       which runs a comparison was built from

Every figure the dashboard shows is read from one of these files, and every
file is hashed in its run's manifest, so a displayed number can always be
traced back to - and downloaded with - the run that produced it.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import os
import platform
import secrets
import subprocess
import sys
import zipfile
from dataclasses import asdict
from typing import Any, Dict, Iterable, List, Optional

from . import events as E
from . import scenarios as S
from .config import RunConfig
from .metrics import DEFINITIONS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPERIMENTS = os.path.join(ROOT, "experiments")
RUNS = os.path.join(EXPERIMENTS, "runs")
COMPARISONS = os.path.join(EXPERIMENTS, "comparisons")
CONFIGURATIONS = os.path.join(EXPERIMENTS, "configurations")

LIVE, REPLAYED, IMPORTED = "live", "replayed", "imported"
RUN_FILES = ("configuration.json", "events.jsonl", "frames.jsonl",
             "decisions.jsonl", "handoffs.jsonl", "summary.json")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# -- code identity -----------------------------------------------------------

def source_files() -> List[str]:
    out = []
    for base in ("echo_sim",):
        for dirpath, dirnames, files in os.walk(os.path.join(ROOT, base)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for f in files:
                if f.endswith(".py"):
                    out.append(os.path.relpath(os.path.join(dirpath, f), ROOT))
    return sorted(out)


def source_hash() -> str:
    """Content hash of the engine code that produces results (echo_sim/)."""
    h = hashlib.sha256()
    for rel in source_files():
        h.update(rel.replace(os.sep, "/").encode() + b"\0")
        with open(os.path.join(ROOT, rel), "rb") as fh:
            h.update(fh.read())
        h.update(b"\0")
    return h.hexdigest()


def _git(*args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                             text=True, timeout=5)
    except Exception:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def code_version() -> Dict[str, Any]:
    commit = _git("rev-parse", "HEAD")
    dirty = None
    if commit:
        dirty = bool(_git("status", "--porcelain", "--", "echo_sim"))
    sh = source_hash()
    return {
        "git_commit": commit,
        "git_dirty": dirty,
        "source_sha256": sh,
        "identifier": (f"{commit[:10]}{'+dirty' if dirty else ''}" if commit
                       else f"src-{sh[:12]}"),
        "note": ("source_sha256 covers every .py file under echo_sim/; "
                 "check with scripts/verify_evidence.py"),
    }


def dependency_versions() -> Dict[str, Optional[str]]:
    from importlib import metadata
    out: Dict[str, Optional[str]] = {
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
    }
    for pkg in ("aioquic", "fastapi", "uvicorn", "cryptography", "numpy",
                "matplotlib"):
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            out[pkg] = None
    return out


# -- configurations ------------------------------------------------------------

def write_configurations() -> None:
    """Keep experiments/configurations/ in step with the scenario definitions."""
    os.makedirs(CONFIGURATIONS, exist_ok=True)
    from .runner import describe_world
    for scen in S.SCENARIOS.values():
        path = os.path.join(CONFIGURATIONS, f"{scen.id}.v{scen.version}.json")
        data = {"scenario": scen.as_dict(), "controller_defaults": asdict(RunConfig()),
                "world": describe_world()}
        data["controller_defaults"].pop("telemetry_path", None)
        blob = json.dumps(data, indent=2, sort_keys=True)
        if not os.path.exists(path) or open(path).read() != blob:
            with open(path, "w") as fh:
                fh.write(blob)


# -- runs --------------------------------------------------------------------

def new_run_id(cfg: RunConfig) -> str:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (f"{stamp}-{cfg.scenario_id}-{E.canonical_mode(cfg.mode)}"
            f"-s{cfg.seed}-{secrets.token_hex(2)}")


class RunRecorder:
    """Owns one run directory from creation to manifest."""

    def __init__(self, cfg: RunConfig, origin: str = LIVE,
                 run_id: Optional[str] = None) -> None:
        self.cfg = cfg
        self.origin = origin
        self.run_id = run_id or new_run_id(cfg)
        self.dir = os.path.join(RUNS, self.run_id)
        os.makedirs(RUNS, exist_ok=True)
        # exist_ok=False: two runs can never write into the same directory.
        os.makedirs(self.dir, exist_ok=False)
        self.started_at = _now()
        self.events_path = os.path.join(self.dir, "events.jsonl")

    def write_configuration(self, configuration: Dict[str, Any]) -> None:
        with open(os.path.join(self.dir, "configuration.json"), "w") as fh:
            json.dump({"run_id": self.run_id, **configuration}, fh, indent=2)

    def finalize(self, summary: Dict[str, Any], status: str,
                 extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return finalize_run_dir(self.dir, self.run_id, summary, status,
                                self.origin, self.started_at, extra or {})


def _read_jsonl(path: str) -> Iterable[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _write_jsonl(path: str, rows: Iterable[Dict[str, Any]]) -> None:
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def finalize_run_dir(run_dir: str, run_id: str, summary: Dict[str, Any],
                     status: str, origin: str, started_at: str,
                     extra: Dict[str, Any]) -> Dict[str, Any]:
    events = list(_read_jsonl(os.path.join(run_dir, "events.jsonl")))
    _write_jsonl(os.path.join(run_dir, "frames.jsonl"),
                 (e for e in events if e["event_type"] in E.FRAME_EVENTS))
    _write_jsonl(os.path.join(run_dir, "decisions.jsonl"),
                 (e for e in events if e["event_type"] in E.DECISION_EVENTS))
    _write_jsonl(os.path.join(run_dir, "handoffs.jsonl"),
                 (e for e in events if e["event_type"] in E.HANDOFF_EVENTS))

    schema_ok, schema_error = True, None
    try:
        E.validate_stream(events)
    except E.SchemaError as exc:
        schema_ok, schema_error = False, str(exc)

    final = next((e for e in reversed(events)
                  if e["event_type"] == "mission_completed"), None)
    with open(os.path.join(run_dir, "summary.json"), "w") as fh:
        json.dump({
            "run_id": run_id,
            "status": status,
            "summary": summary,
            "definitions": DEFINITIONS,
            "faults_fired": (final or {}).get("payload", {}).get("faults_fired", []),
            **extra,
        }, fh, indent=2)

    with open(os.path.join(run_dir, "configuration.json")) as fh:
        conf = json.load(fh)
    files = {}
    for name in RUN_FILES:
        p = os.path.join(run_dir, name)
        if os.path.exists(p):
            files[name] = {"sha256": _sha256(p), "bytes": os.path.getsize(p)}

    manifest = {
        "run_id": run_id,
        "schema_version": E.SCHEMA_VERSION,
        "status": status,
        "data_origin": origin,
        "started_at": started_at,
        "finished_at": _now(),
        "code_version": extra.get("code_version") or code_version(),
        "scenario_id": conf.get("scenario_id"),
        "scenario_version": conf.get("scenario_version"),
        "controller_mode": conf.get("controller_mode"),
        "application_profile": conf.get("application_profile"),
        "seed": conf.get("seed"),
        "duration_s": conf.get("duration_s"),
        "request_rate_hz": conf.get("request_rate_hz"),
        "deadline_ms": conf.get("deadline_ms"),
        "prediction_horizon_s": conf.get("prediction_horizon_s"),
        "network_impairments": {
            "networks": {k: {"best": v["best"], "worst": v["worst"]}
                         for k, v in conf.get("world", {}).get("networks", {}).items()},
            "disturbances": conf.get("scenario", {}).get("disturbances", []),
            "backhaul_ms": conf.get("world", {}).get("backhaul_ms"),
        },
        "processing_delays": {
            "servers": {k: {"base_ms": v["base_processing_ms"],
                            "load_penalty_ms": v["load_penalty_ms"]}
                        for k, v in conf.get("world", {}).get("servers", {}).items()},
            "warmup_frames": conf.get("controller", {}).get("warmup_frames"),
            "warmup_penalty_ms": conf.get("controller", {}).get("warmup_penalty_ms"),
        },
        "faults": conf.get("scenario", {}).get("faults", []),
        "dependencies": extra.get("dependencies") or dependency_versions(),
        "event_count": len(events),
        "schema_valid": schema_ok,
        "schema_error": schema_error,
        "status_labels": conf.get("status", {}),
        "files": files,
    }
    with open(os.path.join(run_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def list_runs() -> List[Dict[str, Any]]:
    out = []
    if not os.path.isdir(RUNS):
        return out
    for rid in sorted(os.listdir(RUNS), reverse=True):
        mpath = os.path.join(RUNS, rid, "manifest.json")
        if not os.path.exists(mpath):
            out.append({"run_id": rid, "status": "in_progress_or_incomplete"})
            continue
        with open(mpath) as fh:
            m = json.load(fh)
        spath = os.path.join(RUNS, rid, "summary.json")
        s = json.load(open(spath)).get("summary", {}) if os.path.exists(spath) else {}
        out.append({k: m.get(k) for k in (
            "run_id", "status", "data_origin", "scenario_id", "scenario_version",
            "controller_mode", "application_profile", "seed", "duration_s",
            "finished_at", "schema_valid")} | {
            "code_version": m.get("code_version", {}).get("identifier"),
            "headline": {k: s.get(k) for k in (
                "p95_ms", "late_results_pct", "results_lost", "longest_gap_ms",
                "completed_handovers", "unnecessary_reversals", "playback_stalls")},
        })
    return out


def run_dir(run_id: str) -> str:
    # Refuse anything that is not a plain directory name under RUNS.
    if not run_id or os.sep in run_id or run_id.startswith(".") or "/" in run_id:
        raise FileNotFoundError(run_id)
    d = os.path.join(RUNS, run_id)
    if not os.path.isdir(d):
        raise FileNotFoundError(run_id)
    return d


def read_json(run_id: str, name: str) -> Dict[str, Any]:
    with open(os.path.join(run_dir(run_id), name)) as fh:
        return json.load(fh)


def read_events(run_id: str, name: str = "events.jsonl") -> List[Dict[str, Any]]:
    return list(_read_jsonl(os.path.join(run_dir(run_id), name)))


def export_zip(run_id: str) -> bytes:
    d = run_dir(run_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(d)):
            z.write(os.path.join(d, name), f"{run_id}/{name}")
    return buf.getvalue()


def verify_run(run_id: str) -> Dict[str, Any]:
    """Recompute file hashes and compare them with the manifest."""
    d = run_dir(run_id)
    m = read_json(run_id, "manifest.json")
    problems = []
    for name, info in m.get("files", {}).items():
        p = os.path.join(d, name)
        if not os.path.exists(p):
            problems.append(f"{name} missing")
        elif _sha256(p) != info["sha256"]:
            problems.append(f"{name} hash mismatch")
    return {"run_id": run_id, "ok": not problems, "problems": problems}


# -- comparisons -------------------------------------------------------------

COMPARISON_FIELDS = (
    "p95_ms", "median_ms", "late_results_pct", "late_by_threshold",
    "results_lost", "longest_gap_ms", "completed_handovers", "failed_handovers",
    "unnecessary_reversals", "server_switches", "path_migrations",
    "time_on_suboptimal_server_s", "continuity_checks_passed",
    "continuity_checks_failed", "reconnects", "requests_generated",
    "playback_stalls", "playback_stall_time_s", "min_buffer_s",
    "state_bytes_transferred", "mean_handoff_ms",
)


def build_comparison(run_ids: List[str], note: str = "",
                     comparison_id: Optional[str] = None) -> Dict[str, Any]:
    rows = {}
    scen = set()
    profiles = set()
    seeds = set()
    for rid in run_ids:
        m = read_json(rid, "manifest.json")
        s = read_json(rid, "summary.json")["summary"]
        scen.add((m["scenario_id"], m["scenario_version"]))
        profiles.add(m.get("application_profile"))
        seeds.add(m.get("seed"))
        rows[m["controller_mode"]] = {
            "run_id": rid, "data_origin": m["data_origin"],
            "code_version": m["code_version"].get("identifier"),
            "status": m["status"],
            "metrics": {k: s.get(k) for k in COMPARISON_FIELDS},
        }
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (sid, sver), = scen if len(scen) == 1 else ((None, None),)
    cid = comparison_id or f"{stamp}-{sid or 'mixed'}-{secrets.token_hex(2)}"
    comp = {
        "comparison_id": cid,
        "created_at": _now(),
        "scenario_id": sid,
        "scenario_version": sver,
        "application_profile": profiles.pop() if len(profiles) == 1 else "mixed",
        "seed": seeds.pop() if len(seeds) == 1 else "mixed",
        "consistent": len(scen) == 1,
        "note": note,
        "modes": {m: rows[m] for m in E.CONTROLLER_MODES if m in rows},
    }
    os.makedirs(COMPARISONS, exist_ok=True)
    with open(os.path.join(COMPARISONS, f"{cid}.json"), "w") as fh:
        json.dump(comp, fh, indent=2)
    return comp


def list_comparisons() -> List[Dict[str, Any]]:
    if not os.path.isdir(COMPARISONS):
        return []
    out = []
    for f in sorted(os.listdir(COMPARISONS), reverse=True):
        if f.endswith(".json"):
            with open(os.path.join(COMPARISONS, f)) as fh:
                out.append(json.load(fh))
    return out


def read_comparison(cid: str) -> Dict[str, Any]:
    if not cid or "/" in cid or os.sep in cid or cid.startswith("."):
        raise FileNotFoundError(cid)
    with open(os.path.join(COMPARISONS, f"{cid}.json")) as fh:
        return json.load(fh)
