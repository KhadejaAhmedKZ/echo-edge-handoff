#!/usr/bin/env python3
"""Build the static GitHub Pages site from a set of recorded runs.

GitHub Pages serves files, not processes, so the live dashboard's FastAPI
backend cannot run there. It does not need to: the dashboard is driven entirely
by the telemetry event stream, so shipping the recorded stream and replaying it
in the browser gives a pixel-identical page. The scene, the agents, the network
readouts and the decision log are all rendered by the same code from the same
events - the only difference is that the events come from a file instead of a
socket.

That distinction is stated on the page itself. A recording presented as a live
experiment would be a lie, and an easy one to catch.

    python3 scripts/build_pages.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "results")
DASHBOARD = os.path.join(ROOT, "dashboard", "index.html")
DOCS = os.path.join(ROOT, "docs")
MODES = ("tcp", "quic", "echo")

# Only the event kinds the dashboard actually consumes.
KEEP = {
    "state", "frame", "run_begin", "run_end", "handoff_begin", "handoff_complete",
    "handoff_failed", "recovery", "quic_migration", "tcp_connection_lost",
    "tcp_reconnected", "handoff_state", "explanation",
}
# Fields on a `state` event that nothing on the page reads.
DROP_STATE = {"ranking", "predicted_best", "intent", "intent_desc",
              "preload_buffer_s", "run_id", "wall", "failed_handoffs"}


def round_floats(obj, nd=3):
    """Shrink the payload without changing anything anyone can see."""
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, dict):
        return {k: round_floats(v, nd) for k, v in obj.items()}
    if isinstance(obj, list):
        return [round_floats(v, nd) for v in obj]
    return obj


def compact(mode: str) -> list:
    path = os.path.join(RESULTS, f"telemetry-{mode}.jsonl")
    if not os.path.exists(path):
        raise SystemExit(f"missing {path} - run scripts/run_experiments.py first")

    raw = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                raw.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # The telemetry file is append-only, so it accumulates every run ever
    # recorded for this mode, including ones that were cancelled part-way.
    # Split it into runs, then pick the one the published results came from,
    # so the replay on the site and the numbers in the report are the same run.
    runs = []
    begin = None
    for i, e in enumerate(raw):
        kind = e.get("kind")
        if kind == "run_begin":
            begin = i
        elif kind == "run_end" and begin is not None:
            runs.append((begin, i))
            begin = None
    if not runs:
        raise SystemExit(f"{path} contains no complete run")

    target = None
    summary_path = os.path.join(RESULTS, f"run-{mode}.json")
    if os.path.exists(summary_path):
        with open(summary_path) as fh:
            want = json.load(fh)["summary"].get("p95_ms")
        for a, b in runs:
            if abs((raw[b].get("p95_ms") or -1) - (want or -2)) < 0.01:
                target = (a, b)
    if target is None:
        # No match: fall back to the longest complete run and say so.
        target = max(runs, key=lambda r: r[1] - r[0])
        print(f"  ! {mode}: no run matched results/run-{mode}.json; "
              f"using the longest of {len(runs)} recorded runs")
    raw = raw[target[0]:target[1] + 1]

    out = []
    for e in raw:
            kind = e.get("kind")
            if kind not in KEEP:
                continue
            e.pop("run_id", None)
            e.pop("wall", None)
            if kind == "state":
                for k in DROP_STATE:
                    e.pop(k, None)
            elif kind == "frame":
                # The sparkline reads one field; the rest is dead weight.
                e = {"kind": "frame", "t": e["t"], "e2e_ms": e.get("e2e_ms")}
            out.append(round_floats(e))

    # Rebase the clock so the replay starts at zero regardless of when the
    # recording was taken.
    if out:
        t0 = out[0]["t"]
        for e in out:
            e["t"] = round(e["t"] - t0, 3)
    return out


# --------------------------------------------------------------------------
# The one part of the page that differs: where events come from.
# --------------------------------------------------------------------------

LIVE_TRANSPORT = """let ws = null;
function connect(){
  ws = new WebSocket('ws://' + location.host + '/ws');
  ws.onmessage = e => { try { handle(JSON.parse(e.data)); } catch(err){} };
  ws.onclose = () => setTimeout(connect, 1200);
}
const send = o => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); };"""

REPLAY_TRANSPORT = """/* Static build: there is no backend here. This page replays a recorded run of
   the real experiment through exactly the same handle() dispatcher the live
   dashboard uses, so what you see is what the run produced. */
const REC = {};
let raf = null, cursor = 0, startedAt = 0, current = null, loading = false;

function halt(){
  if (raf) cancelAnimationFrame(raf);
  raf = null; S.running = false;
}

function drive(events, from){
  halt();
  cursor = from;
  const base = events.length ? events[0].t : 0;
  const offset = events[from] ? events[from].t - base : 0;
  startedAt = performance.now() / 1000 - offset;
  S.running = true;
  function tick(){
    const now = performance.now() / 1000 - startedAt;
    while (cursor < events.length && (events[cursor].t - base) <= now){
      handle(events[cursor++]);
    }
    if (cursor < events.length){ raf = requestAnimationFrame(tick); }
    else { S.running = false; raf = null; setPlay(false); }
  }
  raf = requestAnimationFrame(tick);
  setPlay(true);
}

function setPlay(on){
  const b = el('start');
  b.textContent = on ? 'Pause' : (cursor > 0 ? 'Resume' : 'Play');
}

async function loadMode(m){
  if (REC[m]) return REC[m];
  loading = true;
  el('why').textContent = 'Loading the recorded ' + m + ' run\\u2026';
  const r = await fetch('data/' + m + '.json', {cache: 'force-cache'});
  if (!r.ok) throw new Error('could not load data/' + m + '.json');
  REC[m] = await r.json();
  loading = false;
  return REC[m];
}

async function selectMode(m, autoplay){
  halt(); cursor = 0;
  el('log').innerHTML = ''; S.hist = []; drawSpark();
  try {
    current = await loadMode(m);
  } catch (err){
    el('why').textContent = err.message;
    return;
  }
  // Paint the first state so the page is never an empty shell.
  for (let i = 0; i < current.length && i < 400; i++){
    if (current[i].kind === 'state'){ renderState(current[i]); break; }
  }
  setPlay(false);
  if (autoplay) drive(current, 0);
}

const send = o => {
  if (o.cmd === 'start'){
    if (loading) return;
    if (raf){ halt(); setPlay(false); return; }      // Pause
    if (current) drive(current, cursor);             // Play or Resume
  } else if (o.cmd === 'stop'){
    halt(); setPlay(false);
  } else if (o.cmd === 'replay'){
    if (current) drive(current, 0);
  }
};"""

LIVE_BOOT = "connect(); drawSpark();"
REPLAY_BOOT = "drawSpark(); selectMode(mode, false);"

LIVE_MODE_HANDLER = """    b.classList.add('on'); mode = b.dataset.mode;"""
REPLAY_MODE_HANDLER = """    b.classList.add('on'); mode = b.dataset.mode; selectMode(mode, false);"""

BANNER = """<div class="recbanner">
  <span class="dot"></span>
  <b>Recorded run</b>
  <span class="blurb">replayed in your browser &mdash; the same event stream the live dashboard
    renders, from a real 90&nbsp;s experiment over real QUIC.</span>
  <span class="tip">drag to orbit &middot; scroll to zoom &middot; F toggles follow camera</span>
  <a href="report.html">Read the full report</a>
  <a href="https://github.com/KhadejaAhmedKZ/echo-edge-handoff">Run it live</a>
</div>"""

BANNER_CSS = """.hud{padding-bottom:48px!important}
.recbanner{position:fixed;left:0;right:0;bottom:0;z-index:5;
  display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:center;
  background:var(--panel);border-top:1px solid var(--line);
  padding:9px 16px;font-size:11px;color:var(--muted);height:40px}
.recbanner b{color:var(--white);font-weight:600}
.recbanner .dot{width:6px;height:6px;border-radius:50%;background:var(--orange);flex:none;
  box-shadow:0 0 8px var(--orange)}
.recbanner a{color:var(--orange);text-decoration:none;border-bottom:1px solid currentColor}
.recbanner a:hover{color:var(--white);border-color:var(--white)}
.recbanner .tip{margin-left:auto;color:var(--dim)}
@media (max-width:1100px){.recbanner .blurb{display:none}}
@media (max-width:820px){.recbanner .tip{display:none}}"""


def build_page() -> str:
    html = open(DASHBOARD).read()

    for old, new, label in (
        (LIVE_TRANSPORT, REPLAY_TRANSPORT, "transport"),
        (LIVE_BOOT, REPLAY_BOOT, "boot"),
        (LIVE_MODE_HANDLER, REPLAY_MODE_HANDLER, "mode handler"),
    ):
        if old not in html:
            raise SystemExit(f"could not find the {label} seam in dashboard/index.html")
        html = html.replace(old, new)

    # The live page's hint line is replaced by the recording banner.
    html = html.replace(
        '<div class="hint">drag to orbit &middot; scroll to zoom &middot; F toggles follow camera</div>',
        BANNER)
    html = html.replace("</style>", BANNER_CSS + "\n</style>")
    html = html.replace(
        '<button class="btn primary" id="start">Run</button>',
        '<button class="btn primary" id="start">Play</button>')
    html = html.replace(
        "<title>ECHO - Edge Compute Handoff Orchestration</title>",
        "<title>ECHO - Edge Compute Handoff Orchestration</title>\n"
        '<meta name="description" content="Live 3D replay of the ECHO prototype: an inspection '
        'robot crossing four network zones while its AI inference session migrates between four '
        'edge sites.">\n'
        '<meta property="og:title" content="ECHO - Edge Compute Handoff Orchestration">\n'
        '<meta property="og:description" content="Watch the robot cross four zones while the '
        'inference session moves with it.">')
    return html


def main() -> None:
    os.makedirs(os.path.join(DOCS, "data"), exist_ok=True)

    total = 0
    for mode in MODES:
        events = compact(mode)
        path = os.path.join(DOCS, "data", f"{mode}.json")
        with open(path, "w") as fh:
            json.dump(events, fh, separators=(",", ":"))
        size = os.path.getsize(path)
        total += size
        states = sum(1 for e in events if e["kind"] == "state")
        print(f"  {mode:5s} {len(events):6d} events ({states} state)  {size/1024:8.0f} KB")

    with open(os.path.join(DOCS, "index.html"), "w") as fh:
        fh.write(build_page())
    print(f"  page  {os.path.getsize(os.path.join(DOCS, 'index.html'))/1024:8.0f} KB")
    print(f"  total {total/1024/1024:.2f} MB of recordings")


if __name__ == "__main__":
    main()
