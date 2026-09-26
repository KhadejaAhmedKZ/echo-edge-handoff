#!/usr/bin/env python3
"""Build a static, shareable copy of the mission dashboard for GitHub Pages.

The mission dashboard is a client of a live FastAPI service: twenty-odd REST
endpoints, a WebSocket that streams a running mission, and buttons that start
real experiments. GitHub Pages serves files and runs nothing, so the dashboard
cannot simply be uploaded.

This script does not change the dashboard. It starts the real server, records
what the real API answers, and writes a derived copy into `docs/` where a small
shim answers `fetch` and `WebSocket` from those recordings instead of from a
socket. The page's own HTML, CSS and JavaScript are copied byte for byte apart
from three absolute `/assets/...` URLs that have to become relative to work
under a project Pages path, and one injected `<script>` tag.

What the shared link can and cannot do is therefore precise:

  * Everything that reads - capabilities, scenarios, the run history, the
    comparison tables, per-run summaries - is real recorded output.
  * "Run live experiment" and "Run all four controllers" replay a recorded run
    of the requested controller at its original pace. They do not compute a new
    one, because nothing on Pages can.
  * Runs outside the captured set keep their summary and evidence, but their
    frame-level drill-down is not carried; the shim says so rather than
    failing silently.

Usage:  python3 scripts/build_static_site.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "dashboard")
DOCS = os.path.join(ROOT, "docs")
API_DIR = os.path.join(DOCS, "api")

# Endpoints fetched once when the page loads.
STATIC_GETS = [
    "/api/capabilities",
    "/api/scenarios",
    "/api/status",
    "/api/runs",
    "/api/comparisons",
    "/api/orbit/status",
    "/api/orbit/constellation",
]
MODEL_SAMPLES = 24          # prediction.js walks samples upward, one per click
# Exactly what the page asks for per run - checked against index.html. It
# never requests /frames, /decisions or /handoffs, so carrying them would be
# twenty-odd megabytes nothing reads.
LIGHT_PER_RUN = ["", "/summary", "/verify"]
HEAVY_PER_RUN = ["/recording", "/export"]


# --------------------------------------------------------------------------
# Talking to the real server
# --------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int) -> subprocess.Popen:
    python = os.path.join(ROOT, ".venv", "bin", "python")
    if not os.path.exists(python):
        python = sys.executable
    env = dict(os.environ, ECHO_PORT=str(port))
    proc = subprocess.Popen(
        [python, os.path.join("dashboard", "server.py")],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            urllib.request.urlopen(base + "/api/status", timeout=2).read()
            return proc
        except Exception:
            if proc.poll() is not None:
                raise SystemExit("the dashboard server exited while starting")
            time.sleep(0.5)
    proc.terminate()
    raise SystemExit("the dashboard server did not come up")


def get(base: str, path: str, timeout: float = 90.0):
    """Fetch a path; return (bytes, ok). A failure is recorded, not fatal."""
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return r.read(), True
    except Exception as exc:                       # noqa: BLE001
        note = json.dumps({"static_build": True, "unavailable": str(exc)}).encode()
        return note, False


def slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", path.strip("/")).strip("_") + ".json"


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------


def capture(base: str, path: str, index: dict, stats: dict) -> bytes:
    body, ok = get(base, path)
    name = slug(path)
    with open(os.path.join(API_DIR, name), "wb") as fh:
        fh.write(body)
    index[path] = "api/" + name
    stats["files"] += 1
    stats["bytes"] += len(body)
    if not ok:
        stats["failed"].append(path)
    return body


def pick_featured(comparisons: list) -> tuple[list, list]:
    """Full capture for the richest comparison; replay-only for other 4-mode ones."""
    four = [c for c in comparisons if len(c.get("modes", {})) >= 4]
    four.sort(key=lambda c: (len(c["modes"]), c["created_at"]), reverse=True)
    if not four:
        four = sorted(comparisons, key=lambda c: len(c.get("modes", {})), reverse=True)[:1]

    full, replay = [], []
    if four:
        full = [m["run_id"] for m in four[0]["modes"].values()]
    for c in four[1:]:
        for m in c["modes"].values():
            if m["run_id"] not in full:
                replay.append(m["run_id"])
    return full, replay


def main() -> None:
    port = free_port()
    print(f"starting the real dashboard server on 127.0.0.1:{port} ...")
    proc = start_server(port)
    base = f"http://127.0.0.1:{port}"
    index: dict = {}
    stats = {"files": 0, "bytes": 0, "failed": []}

    try:
        shutil.rmtree(API_DIR, ignore_errors=True)
        os.makedirs(API_DIR, exist_ok=True)

        print("capturing the load-time endpoints ...")
        for path in STATIC_GETS:
            capture(base, path, index, stats)
        for i in range(MODEL_SAMPLES):
            capture(base, f"/api/model/example?sample={i}", index, stats)

        runs = json.loads(open(os.path.join(API_DIR, slug("/api/runs"))).read())
        runs = runs["runs"] if isinstance(runs, dict) else runs
        comps = json.loads(open(os.path.join(API_DIR, slug("/api/comparisons"))).read())
        comps = comps["comparisons"] if isinstance(comps, dict) else comps

        print(f"capturing {len(comps)} comparisons ...")
        for c in comps:
            capture(base, f"/api/comparisons/{c['comparison_id']}", index, stats)

        # Every run the published page lists must work when it is clicked, so
        # the list is narrowed to the runs captured in full rather than left
        # long with rows that fail. These are the runs the four-controller
        # comparisons are built from.
        keep = []
        for c in comps:
            if len(c.get("modes", {})) >= 4:
                for m in c["modes"].values():
                    if m["run_id"] not in keep:
                        keep.append(m["run_id"])
        if not keep:
            keep = [r["run_id"] for r in runs[:8]]

        kept_runs = [r for r in runs if r["run_id"] in keep]
        print(f"capturing {len(kept_runs)} runs in full "
              f"(of {len(runs)} recorded; the rest stay in the repository) ...")
        for n, r in enumerate(kept_runs, 1):
            rid = r["run_id"]
            for suffix in LIGHT_PER_RUN + HEAVY_PER_RUN:
                capture(base, f"/api/runs/{rid}{suffix}", index, stats)
            if n % 5 == 0:
                print(f"  {n}/{len(kept_runs)} runs, {stats['bytes']/1e6:.0f} MB so far")

        # Publish only the runs and comparisons that are complete here.
        with open(os.path.join(API_DIR, slug("/api/runs")), "w") as fh:
            json.dump({"runs": kept_runs}, fh, separators=(",", ":"))
        kept_comps = [c for c in comps
                      if all(m["run_id"] in keep for m in c.get("modes", {}).values())]
        with open(os.path.join(API_DIR, slug("/api/comparisons")), "w") as fh:
            json.dump({"comparisons": kept_comps}, fh, separators=(",", ":"))
        for c in comps:
            if c not in kept_comps:
                f = os.path.join(API_DIR, slug(f"/api/comparisons/{c['comparison_id']}"))
                if os.path.exists(f):
                    os.remove(f)
                index.pop(f"/api/comparisons/{c['comparison_id']}", None)
        print(f"published {len(kept_runs)} runs and {len(kept_comps)} comparisons")
        full, replay = keep, []
        runs = kept_runs

        # Which recording answers a given "start a run" request. The run list
        # carries scenario and controller flat; the per-run detail nests them
        # under "configuration", so the list is the reliable source here.
        by_id = {r["run_id"]: r for r in runs}
        replayable = {}
        for rid in full + replay:
            r = by_id.get(rid, {})
            cfg = r.get("configuration") or {}
            scenario = r.get("scenario_id") or cfg.get("scenario_id")
            mode = r.get("controller_mode") or cfg.get("controller_mode")
            if not scenario or not mode:
                continue
            key = f"{scenario}|{mode}"
            # Prefer a fully captured run when the same key appears twice.
            if key not in replayable or rid in full:
                replayable[key] = rid

        manifest = {
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "index": index,
            "replayable": replayable,
            "full_runs": full,
            "replay_runs": replay,
        }
        with open(os.path.join(API_DIR, "_manifest.json"), "w") as fh:
            json.dump(manifest, fh, separators=(",", ":"))

        print("copying assets and writing the page ...")
        write_site()

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    total = sum(os.path.getsize(os.path.join(dp, f))
                for dp, _, fs in os.walk(DOCS) for f in fs)
    print(f"\ndocs/ is {total/1e6:.1f} MB across {stats['files']} captured responses")
    if stats["failed"]:
        print(f"! {len(stats['failed'])} endpoint(s) answered with an error and were "
              f"recorded as unavailable:")
        for p in stats["failed"][:6]:
            print(f"    {p}")


# --------------------------------------------------------------------------
# The derived page
# --------------------------------------------------------------------------


def write_site() -> None:
    # Refresh the Wi-Fi switch source viewer first: it writes into
    # dashboard/assets, which is copied wholesale just below.
    try:
        import build_wifi_viewer
        build_wifi_viewer.main()
    except SystemExit as exc:
        print(f"  (skipping the Wi-Fi switch viewer: {exc})")

    # Assets are copied verbatim, except that the two absolute /assets/ URLs
    # inside mission.js have to be relative to survive a project Pages path.
    dst_assets = os.path.join(DOCS, "assets")
    shutil.rmtree(dst_assets, ignore_errors=True)
    shutil.copytree(os.path.join(DASHBOARD, "assets"), dst_assets)
    mjs = os.path.join(dst_assets, "mission.js")
    src = open(mjs, encoding="utf-8").read()
    src = src.replace("'/assets/", "'assets/").replace('"/assets/', '"assets/')
    open(mjs, "w", encoding="utf-8").write(src)

    with open(os.path.join(dst_assets, "static-shim.js"), "w", encoding="utf-8") as fh:
        fh.write(SHIM)

    html = open(os.path.join(DASHBOARD, "index.html"), encoding="utf-8").read()
    html = html.replace('href="/assets/', 'href="assets/')
    html = html.replace('src="/assets/', 'src="assets/')
    # The shim must patch fetch and WebSocket before ANY of the page's own code
    # runs - the inline application script fires its first requests well above
    # the module tags at the bottom - so it goes in as the first thing in head.
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    tag = f'<script src="assets/static-shim.js?b={stamp}"></script>'
    m = re.search(r"<head[^>]*>", html, re.I)
    if not m:
        raise SystemExit("could not find <head> to inject the shim into")
    html = html[:m.end()] + "\n" + tag + html[m.end():]
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)

    open(os.path.join(DOCS, ".nojekyll"), "w").close()


SHIM = r"""/* Static build shim - GitHub Pages has no backend.
 *
 * Answers the dashboard's fetch() and WebSocket calls from responses recorded
 * from the real server by scripts/build_static_site.py. The dashboard's own
 * code is unmodified; it cannot tell the difference except that starting a run
 * replays a recorded one instead of computing a new one.
 */
(function () {
  "use strict";

  var realFetch = window.fetch.bind(window);
  var BASE = location.pathname.replace(/[^/]*$/, "");
  // This script is loaded with ?b=<build stamp>. Reusing it on every captured
  // file means a browser that cached a previous build can never serve its old
  // recordings against a new manifest.
  var BUILD = (function () {
    var sc = document.currentScript;
    var m = sc && sc.src ? sc.src.match(/[?&]b=([^&]+)/) : null;
    return m ? m[1] : "";
  })();
  function fileUrl(rel) {
    return BASE + rel + (BUILD ? (rel.indexOf("?") < 0 ? "?b=" : "&b=") + BUILD : "");
  }
  var M = null, READY = null;
  var liveSockets = [];
  var playing = null;          // {runId, timers, startedAt, mode}

  function manifest() {
    if (!READY) {
      READY = realFetch(fileUrl("api/_manifest.json")).then(function (r) {
        if (!r.ok) throw new Error("static manifest missing");
        return r.json();
      }).then(function (m) { M = m; return m; });
    }
    return READY;
  }

  function normalise(url) {
    var u = new URL(url, location.href);
    var p = u.pathname;
    var i = p.indexOf("/api/");
    if (i < 0) return null;
    return p.slice(i) + (u.search || "");
  }

  function jsonResponse(body, status) {
    return new Response(JSON.stringify(body),
      {status: status || 200, headers: {"Content-Type": "application/json"}});
  }

  function notCaptured(path) {
    return jsonResponse({
      static_build: true,
      detail: "This response was not carried into the static build.",
      path: path
    }, 404);
  }

  /* ---- replay ------------------------------------------------------- */

  function recordingUrl(runId) {
    var rel = M.index["/api/runs/" + runId + "/recording"];
    return rel ? fileUrl(rel) : null;
  }

  function stopReplay() {
    if (!playing) return;
    playing.timers.forEach(clearTimeout);
    playing = null;
  }

  function broadcast(evt) {
    liveSockets.forEach(function (s) { s._deliver(evt); });
  }

  function startReplay(runId) {
    stopReplay();
    var url = recordingUrl(runId);
    if (!url) return Promise.reject(new Error("no recording for " + runId));
    // realFetch: the recording lives at a path under api/, so going through
    // the override would bounce it off the index and come back as a 404 body.
    return realFetch(url).then(function (r) { return r.text(); }).then(function (text) {
      var events = text.split("\n").filter(Boolean).map(function (l) {
        try { return JSON.parse(l); } catch (e) { return null; }
      }).filter(Boolean);
      if (!events.length) throw new Error("empty recording");

      playing = {runId: runId, timers: [], startedAt: Date.now()};
      var t0 = events[0].elapsed_time || 0;
      events.forEach(function (evt) {
        var delay = Math.max(0, ((evt.elapsed_time || 0) - t0) * 1000);
        playing.timers.push(setTimeout(function () {
          broadcast(evt);
          if (evt.event_type === "mission_completed") {
            setTimeout(function () { if (playing && playing.runId === runId) playing = null; }, 250);
          }
        }, delay));
      });
      return runId;
    });
  }

  function pickRun(body) {
    var key = (body.scenario_id || "") + "|" + (body.controller_mode || "");
    if (M.replayable[key]) return M.replayable[key];
    // Same controller under a different scenario is the closest honest match.
    var wanted = body.controller_mode;
    for (var k in M.replayable) {
      if (k.split("|")[1] === wanted) return M.replayable[k];
    }
    return M.full_runs[0] || null;
  }

  /* ---- fetch -------------------------------------------------------- */

  window.fetch = function (input, init) {
    var url = (typeof input === "string") ? input
            : (input && input.url) ? input.url : String(input);
    var path = normalise(url);
    if (path === null) return realFetch(input, init);

    var method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
    var body = {};
    if (init && init.body) { try { body = JSON.parse(init.body); } catch (e) { body = {}; } }

    return manifest().then(function () {
      if (method === "POST" && /\/api\/runs\/?$/.test(path.split("?")[0])) {
        var rid = pickRun(body);
        if (!rid) return jsonResponse({detail: "no recorded run available"}, 503);
        return startReplay(rid)
          .then(function () { return jsonResponse({run_id: rid, status: "running"}, 202); })
          .catch(function (e) { return jsonResponse({detail: e.message}, 503); });
      }

      if (method === "POST" && /\/api\/comparisons\/?$/.test(path.split("?")[0])) {
        var order = ["tcp_reconnect", "quic_fixed", "measured", "predictive"];
        var ids = order.map(function (m) {
          return M.replayable[(body.scenario_id || "") + "|" + m];
        }).filter(Boolean);
        if (!ids.length) return jsonResponse({detail: "no recorded comparison"}, 503);
        var chain = startReplay(ids[0]);
        return chain.then(function () {
          return jsonResponse({comparison_id: "static-replay", run_ids: ids}, 202);
        });
      }

      if (method === "POST" && /\/stop$/.test(path)) {
        stopReplay();
        return jsonResponse({stopped: true});
      }

      if (path === "/api/status") {
        return jsonResponse({
          live_run: playing ? {run_id: playing.runId, status: "running"} : null,
          busy: !!playing,
          comparison_job: null
        });
      }

      var rel = M.index[path] || M.index[path.split("?")[0]];
      if (!rel) return notCaptured(path);
      return realFetch(fileUrl(rel)).then(function (r) {
        if (!r.ok) return notCaptured(path);
        return r.blob().then(function (b) {
          var type = /\/(recording|export)$/.test(path)
            ? "application/x-ndjson" : "application/json";
          return new Response(b, {status: 200, headers: {"Content-Type": type}});
        });
      });
    });
  };

  /* ---- download links ------------------------------------------------
   * The runs table links downloads as <a href="/api/runs/.../export">. That
   * is a navigation, not a fetch, so the override above never sees it and on
   * a project Pages path it would resolve to the domain root. Catch the click
   * and hand over the captured file instead.
   */

  document.addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a || a.hasAttribute("download") === false && a.target === "_blank") { /* fall through */ }
    if (!a) return;
    var path = normalise(a.getAttribute("href") || "");
    if (path === null) return;
    e.preventDefault();
    manifest().then(function () {
      var rel = M.index[path];
      if (!rel) {
        alert("That file was not carried into this static build.");
        return;
      }
      realFetch(fileUrl(rel)).then(function (r) { return r.blob(); }).then(function (b) {
        var url = URL.createObjectURL(b);
        var link = document.createElement("a");
        var name = path.replace(/^\/api\/runs\//, "").replace(/\//g, "-");
        link.href = url;
        link.download = name + (/export|recording/.test(path) ? ".jsonl" : ".json");
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
      }).catch(function () { alert("Could not read that file from the static build."); });
    });
  }, true);

  /* ---- WebSocket ---------------------------------------------------- */

  var RealWS = window.WebSocket;

  function FakeWS(url) {
    var self = this;
    this.url = String(url);
    this.readyState = 0;
    this.onopen = this.onmessage = this.onclose = this.onerror = null;
    this._listeners = {};
    liveSockets.push(this);
    setTimeout(function () {
      self.readyState = 1;
      self._emit("open", {});
    }, 0);
  }
  FakeWS.prototype._emit = function (type, evt) {
    var h = this["on" + type];
    if (h) h.call(this, evt);
    (this._listeners[type] || []).forEach(function (fn) { fn.call(this, evt); }, this);
  };
  FakeWS.prototype._deliver = function (obj) {
    if (this.readyState !== 1) return;
    this._emit("message", {data: JSON.stringify(obj)});
  };
  FakeWS.prototype.addEventListener = function (type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  };
  FakeWS.prototype.removeEventListener = function (type, fn) {
    var a = this._listeners[type] || [];
    var i = a.indexOf(fn); if (i >= 0) a.splice(i, 1);
  };
  FakeWS.prototype.send = function () {};
  FakeWS.prototype.close = function () {
    this.readyState = 3;
    var i = liveSockets.indexOf(this);
    if (i >= 0) liveSockets.splice(i, 1);
    this._emit("close", {});
  };
  FakeWS.CONNECTING = 0; FakeWS.OPEN = 1; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;

  window.WebSocket = function (url, protocols) {
    if (/\/api\//.test(String(url))) return new FakeWS(url);
    return new RealWS(url, protocols);
  };
  window.WebSocket.prototype = FakeWS.prototype;
  window.WebSocket.CONNECTING = 0; window.WebSocket.OPEN = 1;
  window.WebSocket.CLOSING = 2; window.WebSocket.CLOSED = 3;

  manifest().catch(function (e) { console.warn("static shim:", e.message); });
})();
"""


if __name__ == "__main__":
    main()
