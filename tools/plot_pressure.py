"""Redraw Fig. 3 at IEEE column width from the unchanged measured inputs."""

import argparse
import json
from pathlib import Path
from statistics import mean

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FormatStrFormatter
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--input", type=Path, default=ROOT / "results/analysis")
parser.add_argument(
    "--output",
    type=Path,
    default=ROOT / "results/figures/pressure",
    help="Output path without extension.",
)
args = parser.parse_args()
DATA = args.input
OUT = args.output
OUT.parent.mkdir(parents=True, exist_ok=True)


plt.rcParams.update(
    {
        "font.family": "DejaVu Serif",
        "font.size": 7.5,
        "text.color": "#151A1E",
        "axes.edgecolor": "#30363B",
        "xtick.color": "#151A1E",
        "ytick.color": "#151A1E",
        "axes.linewidth": 0.55,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "savefig.facecolor": "white",
        "axes.unicode_minus": False,
    }
)

runs = [
    r
    for r in json.loads((DATA / "measured_runs.json").read_text())
    if r["study_role"] == "pressure"
]
summary = json.loads((DATA / "pressure_summary.json").read_text())
loads = [r["target"] for r in summary]
policies = [
    ("zenfs_alloc", "ZenFS-Alloc", "#287DA8", "s", "baseline"),
    ("retainz_pressure", "RetainZ", "#D87921", "o", "method"),
]
# Match the policy colors and pale fills used in the first two figures.
marker_fills = {"s": "#BFD8E5", "o": "#F4D9C1"}
panels = [
    ("completed", "(a) Completed checkpoints (%)", (0, 108), [0, 50, 100]),
    ("host_wa", "(b) Host write amplification", (0.985, 1.305), [1, 1.1, 1.2, 1.3]),
    (
        "mean_invalid_plus_tail_zones",
        "(c) Reserved overhead (zones)",
        (0, 10.6),
        [0, 5, 10],
    ),
    ("checkpoint_p99_ms", "(d) Checkpoint p99 (ms)", (0, 165), [0, 50, 100, 150]),
]


def observations(group, metric):
    if metric == "completed":
        return [
            100
            * r["committed_workload_checkpoints"]
            / r["requested_workload_checkpoints"]
            for r in group
        ]
    if not all(
        r["committed_workload_checkpoints"] == r["requested_workload_checkpoints"]
        for r in group
    ):
        return None
    if metric == "host_wa":
        return [r["warmup_host_wa"] for r in group]
    if metric == "mean_invalid_plus_tail_zones":
        return [r["mean_reserved_overhead_bytes"] / r["zone_bytes"] for r in group]
    return [1000 * r["postwarmup_checkpoint_p99_seconds"] for r in group]


W, H = 252, 189
fig = plt.figure(figsize=(W / 72, H / 72))
legend_handles = [
    Line2D(
        [],
        [],
        color=color,
        marker=marker,
        lw=0.9,
        markersize=3.8 if marker == "s" else 3.2,
        markerfacecolor=marker_fills[marker],
        markeredgewidth=0.7,
        label=label,
    )
    for _, label, color, marker, _ in policies
]
fig.legend(
    handles=legend_handles,
    loc="center",
    bbox_to_anchor=(0.52, 180.5 / H),
    frameon=False,
    ncol=2,
    handlelength=1.9,
    handletextpad=0.45,
    columnspacing=1.4,
    borderaxespad=0,
    fontsize=8,
)

provenance = []
for panel, (metric, title, ylim, yticks) in enumerate(panels):
    col, row = panel % 2, panel // 2
    x = 20 + 126 * col
    bottom = 107 if row == 0 else 27
    height, left_width, gap, right_width = 51, 10, 6, 82
    fig.text(
        (x + 49) / W,
        (bottom + height + 7) / H,
        title,
        ha="center",
        va="baseline",
        fontsize=7.4,
        fontweight="bold",
    )
    axes = [
        fig.add_axes([x / W, bottom / H, left_width / W, height / H]),
        fig.add_axes(
            [(x + left_width + gap) / W, bottom / H, right_width / W, height / H]
        ),
    ]
    for index, ax in enumerate(axes):
        ax.set_ylim(*ylim)
        ax.set_yticks(yticks)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color="#D9DDE0", linewidth=0.35, zorder=0)
        ax.tick_params(axis="both", labelsize=7.2, width=0.5, length=2.0, pad=2)
        if index == 0:
            ax.set_xlim(69.5, 70.5)
            ax.set_xticks([70])
        else:
            ax.set_xlim(79.3, 90.7)
            ax.set_xticks([80, 82, 84, 86, 88, 90])
            ax.spines["left"].set_visible(False)
            ax.tick_params(axis="y", labelleft=False, left=False)
        if metric == "host_wa":
            ax.yaxis.set_major_formatter(FormatStrFormatter("%.1f"))
        # The two disjoint x ranges are explicit; no line crosses the omitted range.
        ax.plot(
            [1 if index == 0 else 0],
            [0],
            transform=ax.transAxes,
            marker=[(-1, -0.65), (1, 0.65)],
            markersize=3.5,
            markeredgewidth=0.65,
            color="#30363B",
            linestyle="none",
            clip_on=False,
            zorder=10,
        )

    for policy, label, color, marker, side in policies:
        records = []
        for load in loads:
            group = [
                r
                for r in runs
                if r["raw_policy"] == policy
                and round(100 * r["occupancy_target"]) == load
            ]
            assert len(group) == 3
            vals = observations(group, metric)
            expected_row = next(r for r in summary if r["target"] == load)
            expected = (
                mean(expected_row[side + "_completed"]) / 250 * 100
                if metric == "completed"
                else expected_row["full_" + side + "_" + metric]
            )
            assert (vals is None) == (expected is None)
            if vals is None:
                continue
            value = mean(vals)
            assert abs(value - expected) < 1e-8, (metric, policy, load, value, expected)
            records.append((load, value, value - min(vals), max(vals) - value))
            provenance.append(
                {
                    "panel": metric,
                    "policy": policy,
                    "load": load,
                    "mean": value,
                    "min": min(vals),
                    "max": max(vals),
                    "cases": [r["case"] for r in group],
                }
            )
        for ax, part in zip(
            axes,
            ([r for r in records if r[0] == 70], [r for r in records if r[0] >= 80]),
        ):
            a = np.array(part)
            ax.errorbar(
                a[:, 0],
                a[:, 1],
                yerr=a[:, 2:].T,
                color=color,
                marker=marker,
                lw=0.85,
                markersize=3.7 if marker == "s" else 3.1,
                markerfacecolor=marker_fills[marker],
                markeredgewidth=0.65,
                elinewidth=0.65,
                capsize=1.6,
                capthick=0.6,
                zorder=3 if marker == "s" else 4,
            )

fig.text(
    0.53, 7 / H, "Target peak live load (%)", ha="center", va="baseline", fontsize=8
)
fig.savefig(
    OUT.with_suffix(".pdf"),
    metadata={
        "Title": "RetainZ: checkpoint performance under capacity pressure",
        "Subject": "42 fixed-reserve FEMU runs; means and min-max across three input seeds. "
        "Broken x-axis between 70% and 80%; 80%-90% uses a linear scale.",
        "Author": "Minxing Chu and Ruoxi Yang",
    },
)
fig.savefig(OUT.with_suffix(".png"), dpi=400)
plt.close(fig)
OUT.with_suffix(".data.json").write_text(json.dumps(provenance, indent=2) + "\n")
print(
    f'Created {OUT.with_suffix(".pdf")} ({W} x {H} pt); verified {len(provenance)} mean/range points.'
)
