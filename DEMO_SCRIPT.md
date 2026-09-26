# 1–2 minute video: one mission, one moment, the evidence

Record from **recorded replay** (no timing risk). Settings: scenario **C - Target
cannot prepare**, controller **Measurement-driven migration**, 4× replay speed
until the first test event, then 1×.

| Time | Screen | Say |
|---|---|---|
| 0:00 | First screen, intro card | "A robot inspects a site. It sends camera frames to a nearby server and needs each answer back in 150 milliseconds. As it moves, its networks change underneath it." |
| 0:12 | Press **Replay recorded mission**. Point at the badge *RECORDED REPLAY* and the four labels at top right | "This is a recorded run of the real experiment. The QUIC transport is real; the network conditions are emulated; the processing is simulated. It says so on screen." |
| 0:22 | Map: Wi-Fi lane fading, robot moving; chart: points rising toward the deadline | "Wi-Fi is fading. Response time is creeping toward the deadline line." |
| 0:32 | Dashed orange line to Server B; *Test event: target preparation failure* | "ECHO picks Server B and starts preparing it - while Server A keeps serving. This is a deliberate test: we told B to refuse." |
| 0:42 | Handover panel shows **Prepare ✕**; timeline: *Old server kept serving* | "The preparation fails. Nothing is lost: the session never left A, and results keep arriving." |
| 0:52 | Second attempt: Prepare → Transfer → Check → Switch → Finish old work, all ✓ | "A moment later B is ready. The session is copied, B proves it works, and only then does it take over." |
| 1:02 | Continuity panel: *The server changed. The session identity and progress continued.* | "Same session, state version carried over, checked before and after the switch." |
| 1:12 | Scroll to **Comparison results**, threshold 150 ms | "Same mission, same seed, four controllers. TCP reconnect loses results and rebuilds the session. QUIC with a fixed server never drops the connection but stays on the wrong server. Both ECHO modes keep late results low - and prediction is not clearly better than plain measurement." |
| 1:30 | Click a number → provenance box → **Download evidence** | "Every number opens the run that produced it: its configuration, its code version, its full event log." |
| 1:40 | End | "ECHO: prepare the next server before you need it, and prove the session survived the move." |

Say numbers only as they appear on screen; they come from `experiments/RESULTS.md`.
