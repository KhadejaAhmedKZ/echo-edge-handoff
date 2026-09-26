/* Static build shim - GitHub Pages has no backend.
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
  var M = null, READY = null;
  var liveSockets = [];
  var playing = null;          // {runId, timers, startedAt, mode}

  function manifest() {
    if (!READY) {
      READY = realFetch(BASE + "api/_manifest.json").then(function (r) {
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
    return rel ? BASE + rel : null;
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
      return realFetch(BASE + rel).then(function (r) {
        if (!r.ok) return notCaptured(path);
        return r.blob().then(function (b) {
          var type = /\/(recording|export)$/.test(path)
            ? "application/x-ndjson" : "application/json";
          return new Response(b, {status: 200, headers: {"Content-Type": type}});
        });
      });
    });
  };

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
