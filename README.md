# ECHO Inspection Mission

**ECHO keeps a moving inspection application responsive by selecting a suitable
network and processing server, preparing the target, and transferring session
progress before switching.**

This folder is the submitted prototype: one mission, one dashboard, one engine,
and the recorded evidence behind every number it shows. The macOS, Linux,
Wi-Fi-switch, QUIC-spike and satellite projects in `ECHO_Project/` are
supporting experiments; they are listed in the dashboard's *Implementation
status* panel and are not part of this mission.

## Start

```bash
./start.sh                       # first run creates .venv; then opens http://127.0.0.1:8080
```

The first screen explains the mission. **Replay recorded mission** plays a
recorded run with no timing risk and no external service; **Run it live** runs
the real experiment (about 90 s). Everything is local: no CDN, no fonts, no API
keys.

Tests: `.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q`

## What a judge sees

The dashboard answers five questions, top to bottom:

| Question | Panel |
|---|---|
| What is the robot doing? | Mission route: robot, zones, network coverage lanes, the four servers |
| What is changing? | Network and server cards; test events are labelled as test events |
| What is ECHO deciding? | Current decision + plain explanation; candidate scores on demand |
| Is the application still meeting its deadline? | Application experience + response-time chart with the deadline line |
| How does this compare with the alternatives? | Comparison table; every number opens the run that produced it |

The moment to watch: the connection deteriorates, a **dashed line** reaches to
the server being prepared while the old one keeps serving, the handover steps
tick through **Prepare → Transfer → Check → Switch → Finish old work**, and the
continuity panel reads *"The server changed. The session identity and progress
continued."* - with the response-time chart showing results still arriving.

Visual states are driven only by engine events: the dashed line appears on the
controller's `handoff_phase_changed`, never on a timer. "Robot moved into
another zone" and "controller selected another server" are separate lines in the
decision timeline, because they are not the same thing.

## Status of every part

| Part | Status |
|---|---|
| QUIC transport, connection migration (aioquic) | **real**, on loopback |
| TCP reconnect baseline | **real** sockets |
| Network conditions (delay, jitter, loss, bandwidth, outages) | **emulated** by userspace relays |
| Processing on the servers | **simulated** delay that responds to load and cold starts; no detector runs |
| Session-state transfer + continuity checks | **implemented**; the tracking content is synthetic, so no tracking accuracy is claimed |
| Satellite-like backup path | **emulated** link profile; no satellite terminal |
| Application profile | **selected explicitly**; automatic app recognition from encrypted traffic is not claimed |
| Buffered playback | **controlled synthetic workload** (not a video player, not YouTube) |
| Explanations | **deterministic** sentences from the decision's own numbers; no language model |
| Public Wi-Fi / captive portal / VPN | **proposed**, not implemented; the mission assumes managed infrastructure |

## Controller modes (same engine, same scenario, same seed)

| Mode | What it does |
|---|---|
| `tcp_reconnect` - TCP reconnect baseline | Keeps one TCP connection until it breaks, reconnects, rebuilds the session from nothing |
| `quic_fixed` - QUIC with fixed server | The connection follows the robot across networks; processing never moves |
| `measured` - Measurement-driven migration | ECHO migration on smoothed current measurements (forecast horizon 0). **Demonstration default** |
| `predictive` - Predictive migration | ECHO migration on a 2 s trend forecast. Experimental option |

Measurement-driven is the default because it is the conservative choice and the
recorded runs do not show prediction winning consistently - see
`experiments/RESULTS.md`, generated from the comparisons.

## Scenarios

| Scenario | What happens | What it tests |
|---|---|---|
| A - Gradual coverage change | Lab → yard → dock; Wi-Fi fades, 5G and the dock take over | The core mechanism |
| B - Brief disturbance | In the lab, Wi-Fi gets a ~4 s delay/loss spike and recovers | Whether the controller avoids switching away and straight back |
| C - Target cannot prepare | Scenario A, but the first prepared target refuses (deterministic **test event**) | Staying on the usable old server and reporting the failure |
| D - Only the backup path is left | In the yard, 5G is cut; only the high-delay backup path remains | Using the backup only when it is the only route; reachable is not the same as on time |

Scenario definitions live in `echo_sim/scenarios.py` and are versioned; the
version is recorded in every run's manifest and in `experiments/configurations/`.

## Metrics (one implementation: `echo_sim/metrics.py`)

| Metric | Definition |
|---|---|
| Current response time | duration of the latest completed request |
| Late results | lost results plus completed ones slower than the analysis deadline, as a share of all requests |
| p95 response time | 95th percentile over completed requests |
| Lost results | no usable result within 2.5 s, or dropped because 48 requests were already waiting |
| Longest result gap | longest interval between two consecutive results |
| Completed handovers | server migrations that passed every step |
| Unnecessary reversals | a server switch back to the server just left, within 10 s |
| Time on a server that was not the best | judged by a measurement-only reference scorer, identical for every mode |

The deadline (100 / 150 / 200 ms) is an **analysis** threshold. Changing it in
the dashboard never changes controller behaviour. The dashboard never computes
its own version of a metric: it displays `metrics_updated` events (live) and
`summary.json` (recorded), both produced by `RunMetrics.summary()`.

## Evidence

```
experiments/
  configurations/            scenario + controller definitions, versioned
  runs/<run-id>/             configuration.json, events.jsonl, frames.jsonl,
                             decisions.jsonl, handoffs.jsonl, summary.json, manifest.json
  comparisons/<id>.json      which runs each comparison was built from
  RESULTS.md                 generated tables for the report
```

Each manifest records the run id, engine code identity (git commit when
available, and a SHA-256 of `echo_sim/`), scenario id and version, controller
mode, profile, seed, duration, request rate, network impairments, processing
delays, deadline, dependency versions, whether the data is **live**,
**replayed** or **imported**, and the SHA-256 of every file.

```bash
.venv/bin/python scripts/run_experiments.py            # all scenarios x all modes -> runs + comparisons
.venv/bin/python scripts/report_tables.py              # regenerate experiments/RESULTS.md
.venv/bin/python scripts/verify_evidence.py            # re-hash every run against its manifest
.venv/bin/python scripts/import_legacy.py              # import results/run-*.json (pre-manifest) as "imported"
```

The original README table (`results/run-*.json`, 90 s, seed 7) is kept as three
**imported** runs, recomputed with the shared metrics and labelled as having no
recorded code version. It has no measurement-driven run.

## Telemetry contract (`echo_sim/events.py`)

Every event: `run_id, sequence_number, elapsed_time, event_type,
controller_mode, scenario_id, source_component, payload`. Types include
`mission_started, network_observed, zone_changed, candidate_scored,
decision_made, handoff_phase_changed, handoff_completed, handoff_failed,
state_verified, request_completed, request_lost, fault_injected,
metrics_updated, mission_completed`. Every event is validated when emitted; a
violation is recorded as `agent_error`, never silently dropped. The dashboard,
replay, evidence files and result tables all read this one stream.

## API (`dashboard/server.py`)

| Endpoint | Purpose |
|---|---|
| `GET /api/capabilities` | implementation status, modes, profiles |
| `GET /api/scenarios` | scenario definitions and the static world |
| `POST /api/runs` | start one live run (bounded, validated config; 409 if one is running) |
| `GET /api/runs`, `GET /api/runs/{id}` | list; status + configuration + manifest |
| `POST /api/runs/{id}/stop` | stop; partial evidence is kept with status `stopped` |
| `GET /api/runs/{id}/summary`, `/frames`, `/decisions`, `/handoffs` | the evidence files |
| `GET /api/runs/{id}/recording` | complete `events.jsonl`, for replay |
| `GET /api/runs/{id}/export`, `/verify` | zip download; re-hash against manifest |
| `POST /api/comparisons`, `GET /api/comparisons[/{id}]` | run all modes, or assemble from run ids |
| `WS /api/runs/{id}/events`, `WS /api/live/events` | live telemetry |

Only one live experiment runs at a time (the relays bind fixed local ports), and
every run writes into a new directory that is created exclusively, so two runs
can never overwrite each other.

## Engine fixes made for this version

* **Activity classification had an unreachable branch.** The broad "inference"
  rule matched first, so "realtime call" could never be returned. Rules are now
  disjoint, tested, return `unknown` when unsure, and `unknown` uses the
  conservative (live) policy. The classifier only advises; the profile is
  selected explicitly.
* **The robot's own session made its current server look worse.** Scoring
  counted this session's load against the server hosting it, so the other
  server looked better right after every switch - the cause of the A↔B↔A
  switching in the original run. Every server is now scored as if it hosted the
  session.
* **No discretionary switch before the Watcher has 8 measurements (2 s)**, and
  **returning to a server just left needs a 25 % margin held for 3× as long**
  (not applied when the server was left because its path died).
* **When the current network dies, move the connection first.** The session
  stays on its server and the QUIC connection migrates to the best surviving
  network at once (no session work); a server change is then considered
  make-before-break while that path serves. Reachability now also requires a
  live measurement, because the smoothed level lagged an abrupt outage by
  about 1.5 s.
* **Finish old work is real**: the old server receives `ECHO_DRAIN` with the
  last request number already sent, finishes those, and refuses new ones.
* **Hosts without IPv6** (some containers/VMs) could not open QUIC connections,
  because aioquic's client insists on a dual-stack socket. The client now uses
  IPv4 when IPv6 is unavailable.

## Known limitations (stated, not hidden)

* **One run per controller per scenario (seed 7).** The comparisons are single
  runs, not distributions. Run other seeds with `--seed`.
* **The high-delay backup path overwhelms QUIC at 20 requests/s.** With about
  620 ms round trip and 1 % loss, QUIC's congestion control cannot sustain the
  request rate; results queue and some time out (scenario D). ECHO does not yet
  lower the request rate on such a path.
* **The TCP relay turns loss into delay** (dropping bytes would corrupt the
  stream), while the UDP relay drops packets and QUIC reacts to the losses. On
  the lossy backup path this treats TCP more kindly than a real network would.
* **Scenario B:** both ECHO modes still switch away during the spike (and on
  natural Wi-Fi dips) and come back; the dashboard counts these as
  unnecessary reversals. The buffered-playback profile, which can lean on its
  buffer, does not switch.
* All four servers run in one process; server health is read in-process, not
  probed over the network.
* `scripts/build_pages.py` and `scripts/plot_results.py` still read the
  original `results/` files for the older GitHub Pages site.

## Layout

```
echo_sim/
  events.py        the telemetry contract          scenarios.py   scenarios + fault injector
  telemetry.py     validated event bus             evidence.py    run dirs, manifests, comparisons
  world.py         route, coverage, disturbances   runner.py      one run, recorded
  netem.py         impairment relays               client.py      robot client + 4 controller modes
  edge.py          processing servers              metrics.py     the one metrics implementation
  transport/       quic.py, tcp.py                 agents/        watcher, predictor, intent,
                                                                  decision, orchestrator, handoff,
                                                                  recovery, troubleshooter, explainer
dashboard/         server.py (API) + index.html (the mission dashboard, no external assets)
scripts/           run_experiments, report_tables, verify_evidence, import_legacy
experiments/       the evidence
docs/              the earlier GitHub Pages site (original 3D replay; not updated)
```


## 25 September: mission control update

The default view now shows a yellow EDGE-lettered quadruped travelling through the inspection site, four processing towers, network measurements and a plain-language controller panel. Earth and its satellites remain visible in an inset. Expand Earth is optional. Both views are local; no CDN or API key is required.

The globe uses 3,703 positions propagated from the existing partial CelesTrak catalog to **21 September 2026, 07:31:30 UTC**. It is a dated snapshot, not live satellite tracking. Three selected satellite models and orbital traces reuse the older globe's visual components. Orbit geometry does not control the inspection simulation. The robot is a procedural illustration with an EDGE text label, not an official robot CAD model or validated brand logo.

The robot's position, network connection, server highlight and preparation link follow the run's telemetry. Recorded replay is labelled. A connected but slow result displays “Connected · deadline unmet”. No detector or physical robot is controlled.

The controller now resets interrupted challenger evidence, so alternating candidates cannot accumulate votes across unrelated checks. A five-second sustained-benefit guard was also tested over full-length scenario B with seeds 7, 17 and 27. It did not eliminate reversals and incurred extra late results. Consequently **that guard is disabled by default**; it remains an experimental RunConfig option, stable_link_hold_s. Existing recordings are retained with their original source hashes. Do not describe those recordings as results from the new default code.

Repeated experiments:

```sh
.venv/bin/python scripts/run_validation_suite.py --seeds 7 17 27
# Optional experimental guard, explicitly separate from the default:
.venv/bin/python scripts/run_validation_suite.py --scenarios brief_disturbance --modes measured predictive --seeds 7 17 27 --stable-link-hold 5
```

The suite records every run, creates paired comparisons and saves descriptive mean, median, sample standard deviation and range for per-run metrics in experiments/suites. It refuses source changes within a suite. Its sample statistics do not establish statistical significance. Full default validation across all scenarios remains necessary before submission.

Remaining engineering limitations: automatic backup-path workload reduction and local inference fallback are not implemented; all edge services still share one process; TCP loss approximation differs from QUIC packet loss. These are disclosed rather than presented as completed improvements. Baselines are laboratory controls, not measurements of current UAE deployments.

## Scroll-down Earth and connected prediction services

The Earth now has a permanent full-width section below the inspection mission; no view switch is needed. The mission server starts the existing sibling 06_satellite-dashboard gateway and model worker when available, and proxies three fixed local endpoints:

- `/api/orbit/status`: N2YO orbital elements and SGP4 visibility prediction for the fixed Abu Dhabi observer.
- `/api/orbit/constellation`: CelesTrak catalog propagated to the current time; stale/partial catalog labels are preserved.
- `/api/model/example?sample=0`: actual Random Forest inference on labelled held-out cellular dataset examples.

The existing N2YO_API_KEY is read from the project or satellite dashboard .env into the server-side gateway only. No credential is copied into frontend assets. The initial dated globe snapshot remains an explicitly labelled fallback if the gateway cannot provide a catalog. Satellite tracking targets and candidates are drawn from the API response. These advisory forecasts do not drive mission handovers. Moving robot GPS, a matching signal-telemetry adapter and end-to-end validation are still required for that integration.

Verified on this Mac: N2YO status returned a target, Random Forest inference returned a probability, and the constellation returned 3,699 propagated positions with stale orbital elements correctly flagged. 68 automated tests pass. Preview for this editing session uses port 8081 because the older server still owns 8080.
