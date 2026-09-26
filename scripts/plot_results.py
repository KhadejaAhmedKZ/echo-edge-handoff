#!/usr/bin/env python3
"""Turn the three recorded runs into the figures used in the write-up."""
from __future__ import annotations

import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")

BG = "#05020c"
PANEL = "#150a2c"
TEXT = "#ede9fe"
MUTED = "#9d8fc0"
MODE_COLOUR = {"tcp": "#ff7a2f", "quic": "#8b5cf6", "echo": "#c4b5fd"}
MODE_LABEL = {"tcp": "TCP baseline", "quic": "QUIC only", "echo": "QUIC + ECHO"}


def style(ax) -> None:
    ax.set_facecolor(PANEL)
    for spine in ax.spines.values():
        spine.set_color("#4c1d95")
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.xaxis.label.set_color(MUTED)
    ax.yaxis.label.set_color(MUTED)
    ax.title.set_color(TEXT)
    ax.grid(True, color="#2a1152", linewidth=.6, alpha=.8)
    ax.set_axisbelow(True)


def load(mode: str):
    path = os.path.join(RESULTS, f"run-{mode}.json")
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def main() -> None:
    runs = {m: load(m) for m in ("tcp", "quic", "echo")}
    runs = {m: r for m, r in runs.items() if r}
    if not runs:
        print("no results found - run scripts/run_experiments.py first")
        return

    # ---------------- figure 1: latency over the whole route ----------------
    fig, axes = plt.subplots(len(runs), 1, figsize=(12, 3.1 * len(runs)),
                             sharex=True, facecolor=BG)
    if len(runs) == 1:
        axes = [axes]
    for ax, (mode, run) in zip(axes, runs.items()):
        style(ax)
        frames = run["frames"]
        ok = [(f["sent_t"], f["e2e_ms"]) for f in frames if f["e2e_ms"] is not None]
        lost = [f["sent_t"] for f in frames if f["lost"]]
        ax.plot([t for t, _ in ok], [v for _, v in ok], color=MODE_COLOUR[mode],
                linewidth=.9, label="end-to-end inference latency")
        for a, b in run.get("crossing_windows", []):
            ax.axvspan(a, b, color="#8b5cf6", alpha=.11, linewidth=0)
        for t in lost:
            ax.axvline(t, color="#ff7a2f", alpha=.30, linewidth=.7)
        s = run["summary"]
        ax.set_title(f"{MODE_LABEL[mode]} - p95 {s['p95_ms']} ms, "
                     f"zone gap {s['zone_gap_ms']} ms, {s['frames_lost']} frames lost, "
                     f"{s['time_on_suboptimal_edge_s']} s on a suboptimal edge",
                     fontsize=11, loc="left")
        ax.set_ylabel("ms")
        ax.set_ylim(0, min(1400, max((v for _, v in ok), default=500) * 1.1))
    axes[-1].set_xlabel("seconds along the route  (shaded = zone crossing)")
    handles = [Patch(facecolor="#8b5cf6", alpha=.3, label="zone crossing"),
               Patch(facecolor="#ff7a2f", alpha=.5, label="lost frame")]
    axes[0].legend(handles=handles, loc="upper right", fontsize=8,
                   facecolor=PANEL, edgecolor="#4c1d95", labelcolor=MUTED)
    fig.suptitle("Inference latency across four zones, identical route and workload",
                 color=TEXT, fontsize=13, y=.995)
    fig.tight_layout()
    out1 = os.path.join(RESULTS, "latency-timeline.png")
    fig.savefig(out1, dpi=150, facecolor=BG)
    print("wrote", out1)

    # ---------------- figure 2: the headline comparison ---------------------
    fig, axes = plt.subplots(1, 4, figsize=(15, 4.2), facecolor=BG)
    modes = list(runs.keys())
    colours = [MODE_COLOUR[m] for m in modes]
    labels = [MODE_LABEL[m] for m in modes]

    panels = [
        ("Inference latency gap\n(p95 crossing - p95 settled)", "zone_gap_ms", "ms"),
        ("p95 end-to-end latency", "p95_ms", "ms"),
        ("Longest gap with no result", "longest_gap_ms", "ms"),
        ("Time on a suboptimal edge", "time_on_suboptimal_edge_s", "s"),
    ]
    for ax, (title, key, unit) in zip(axes, panels):
        style(ax)
        vals = [runs[m]["summary"].get(key) or 0 for m in modes]
        bars = ax.bar(labels, vals, color=colours, width=.62)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height(),
                    f"{v:.0f}{unit}", ha="center", va="bottom",
                    color=TEXT, fontsize=10, fontweight="bold")
        ax.set_title(title, fontsize=10.5)
        ax.set_ylim(0, max(vals) * 1.25 if max(vals) else 1)
        ax.tick_params(axis="x", labelrotation=12)
    fig.suptitle("The success criterion is not 'the connection stayed alive'",
                 color=TEXT, fontsize=13)
    fig.tight_layout()
    out2 = os.path.join(RESULTS, "comparison.png")
    fig.savefig(out2, dpi=150, facecolor=BG)
    print("wrote", out2)

    # ---------------- figure 3: latency distribution ------------------------
    fig, ax = plt.subplots(figsize=(9, 4.6), facecolor=BG)
    style(ax)
    for mode, run in runs.items():
        vals = sorted(f["e2e_ms"] for f in run["frames"] if f["e2e_ms"] is not None)
        if not vals:
            continue
        ys = [i / (len(vals) - 1) * 100 for i in range(len(vals))]
        ax.plot(vals, ys, color=MODE_COLOUR[mode], linewidth=2.2, label=MODE_LABEL[mode])
    ax.axhline(95, color=MUTED, linestyle="--", linewidth=.8)
    ax.text(ax.get_xlim()[1], 95.6, "p95", color=MUTED, fontsize=9, ha="right")
    ax.set_xlabel("end-to-end inference latency (ms)")
    ax.set_ylabel("percentile")
    ax.set_title("Latency distribution over the whole route", fontsize=12, loc="left")
    ax.legend(facecolor=PANEL, edgecolor="#4c1d95", labelcolor=TEXT, fontsize=10)
    fig.tight_layout()
    out3 = os.path.join(RESULTS, "latency-cdf.png")
    fig.savefig(out3, dpi=150, facecolor=BG)
    print("wrote", out3)


if __name__ == "__main__":
    main()
