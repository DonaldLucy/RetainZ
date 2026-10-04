"""Compact, single-column Figure 4 from the original mechanism measurements."""

import argparse
import json
from pathlib import Path
from statistics import mean

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.ticker import FormatStrFormatter

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--input", type=Path, default=ROOT / "results/analysis")
parser.add_argument(
    "--output",
    type=Path,
    default=ROOT / "results/figures/mechanisms",
    help="Output path without extension.",
)
args = parser.parse_args()
DATA = args.input
OUT = args.output
OUT.parent.mkdir(parents=True, exist_ok=True)


INK, RULE, GRID = "#151A1E", "#30363B", "#D9DDE0"
BLUE, ORANGE, GRAY = "#287DA8", "#D87921", "#707B86"
BLUE_FILL, ORANGE_FILL, GRAY_FILL = "#BFD8E5", "#F4D9C1", "#D4D8DD"
plt.rcParams.update(
    {
        "font.family": "DejaVu Serif",
        "font.size": 7.5,
        "text.color": INK,
        "axes.edgecolor": RULE,
        "xtick.color": INK,
        "ytick.color": INK,
        "axes.linewidth": 0.55,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "axes.unicode_minus": False,
        "savefig.facecolor": "white",
    }
)

cache = json.loads((DATA / "cache_comparison.json").read_text())
hints = json.loads((DATA / "hint_precision.json").read_text())
admission = [
    r
    for r in json.loads((DATA / "measured_runs.json").read_text())
    if r["study_role"] == "replacement_pressure"
]
assert len(admission) == 6 and all(
    r["committed_workload_checkpoints"] == r["requested_workload_checkpoints"]
    for r in admission
)
W, H = 252, 94.5
fig = plt.figure(figsize=(W / 72, H / 72))
provenance = []


def summarize(panel, condition, vals):
    assert len(vals) == 3
    v, lo, hi = mean(vals), min(vals), max(vals)
    provenance.append(dict(panel=panel, condition=condition, mean=v, min=lo, max=hi))
    return v, v - lo, hi - v


def axes(x, y, w, h, grid="y"):
    ax = fig.add_axes([x / W, y / H, w / W, h / H])
    ax.spines[["top", "right"]].set_visible(False)
    ax.tick_params(axis="both", labelsize=6.8, width=0.5, length=2, pad=1.5)
    ax.tick_params(axis="x", labelsize=6.4)
    ax.set_axisbelow(True)
    ax.grid(axis=grid, color=GRID, linewidth=0.35)
    return ax


# (a): keep the legend local; narrow grouped bars leave room for numeric labels.
fig.text(
    42 / W, 84.5 / H, "(a) Cached summaries", ha="center", fontsize=7, fontweight="bold"
)
fig.text(45 / W, 76 / H, "Allocation time (ms/commit)", ha="center", fontsize=6.6)
handles = [
    Patch(facecolor=GRAY_FILL, edgecolor=GRAY, linewidth=0.7, label="Scan"),
    Patch(facecolor=ORANGE_FILL, edgecolor=ORANGE, linewidth=0.7, label="Summary"),
]
fig.legend(
    handles=handles,
    loc="center",
    bbox_to_anchor=(48 / W, 66 / H),
    frameon=False,
    ncol=2,
    fontsize=6.8,
    handlelength=0.85,
    handleheight=0.65,
    handletextpad=0.35,
    columnspacing=0.65,
    borderaxespad=0,
)
ax = axes(13, 23, 64, 37)
ax.set_xlim(-0.5, 1.5)
ax.set_xticks([0, 1], labels=["70%", "84%"])
ax.set_ylim(0, 8.3)
ax.set_yticks([0, 3, 6])
for target, x in [(70, 0), (84, 1)]:
    group = [r for r in cache if r["target"] == target]
    assert all(r["placement_events_equal"] for r in group)
    for key, offset, edge, fill in [
        ("scan", -0.16, GRAY, GRAY_FILL),
        ("cached", 0.16, ORANGE, ORANGE_FILL),
    ]:
        vals = [r[key + "_allocator_ms"] for r in group]
        v, low, high = summarize("cached summaries", f"{target}% {key}", vals)
        if key == "cached":
            assert all(
                abs(
                    r["cached_allocator_ms"]
                    - r["accounted_selector_ms"]
                    - r["summary_update_ms"]
                )
                < 1e-10
                for r in group
            )
        xx = x + offset
        ax.bar(
            xx,
            v,
            width=0.17,
            color=fill,
            edgecolor=edge,
            linewidth=0.65,
            yerr=[[low], [high]],
            capsize=1.3,
            error_kw={"ecolor": edge, "elinewidth": 0.65, "capthick": 0.6},
            zorder=3,
        )
        ax.annotate(
            f"{v:.3f}",
            (xx, v + high),
            xytext=(0, 2),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=6.7,
        )
fig.text(45 / W, 6 / H, "Target load", ha="center", fontsize=6.5)

# (b): the same RetainZ allocator receives exact or coarsened finite hints.
fig.text(
    129 / W, 84.5 / H, "(b) Hint precision", ha="center", fontsize=7, fontweight="bold"
)
fig.text(130 / W, 76 / H, "Reserved overhead (zones)", ha="center", fontsize=6.6)
fig.text(130 / W, 64 / H, "84% load", ha="center", fontsize=6.8)
ax = axes(98, 23, 65, 37)
ax.set_xlim(-0.6, 2.6)
ax.set_xticks([0, 1, 2], labels=["Exact", "4", "16"])
ax.set_ylim(0, 8.3)
ax.set_yticks([0, 3, 6])
for x, width in enumerate((1, 4, 16)):
    vals = [r["overhead_zones"] for r in hints if r["width"] == width and r["complete"]]
    v, low, high = summarize("hint precision", f"grid {width}", vals)
    ax.bar(
        x,
        v,
        width=0.25,
        facecolor=ORANGE_FILL,
        edgecolor=ORANGE,
        linewidth=0.7,
        yerr=[[low], [high]],
        capsize=1.4,
        error_kw={"ecolor": ORANGE, "elinewidth": 0.7, "capthick": 0.6},
        zorder=3,
    )
    ax.annotate(
        f"{v:.2f}",
        (x, v + high),
        xytext=(0, 2),
        textcoords="offset points",
        ha="center",
        va="bottom",
        fontsize=6.7,
    )
fig.text(130 / W, 6 / H, "Hint grid (epochs)", ha="center", fontsize=6.5)

# (c): shared transactional admission lets every input complete at 90% load.
fig.text(216 / W, 84.5 / H, "(c) Admission", ha="center", fontsize=7, fontweight="bold")
fig.text(216 / W, 76 / H, "Host write amplification", ha="center", fontsize=6.6)
fig.text(216 / W, 64 / H, "90% load", ha="center", fontsize=6.8)
ax = axes(183, 23, 66, 37)
ax.set_xlim(-0.5, 1.5)
ax.set_xticks([0, 1], labels=["ZenFS-Alloc", "RetainZ"])
ax.set_ylim(1, 1.85)
ax.set_yticks([1, 1.3, 1.6])
ax.yaxis.set_major_formatter(FormatStrFormatter("%.1f"))
for x, policy, edge, fill, marker in [
    (0, "zenfs_alloc", BLUE, BLUE_FILL, "s"),
    (1, "retainz_pressure", ORANGE, ORANGE_FILL, "o"),
]:
    vals = [r["warmup_host_wa"] for r in admission if r["raw_policy"] == policy]
    v, low, high = summarize("admission", policy, vals)
    ax.errorbar(
        x,
        v,
        yerr=[[low], [high]],
        marker=marker,
        markersize=3.5,
        color=edge,
        markerfacecolor=fill,
        markeredgewidth=0.7,
        elinewidth=0.7,
        capsize=1.6,
        capthick=0.65,
        zorder=3,
    )
    ax.annotate(
        f"{v:.3f}",
        (x, v + high),
        xytext=(0, 2),
        textcoords="offset points",
        ha="center",
        va="bottom",
        fontsize=6.7,
    )

fig.savefig(
    OUT.with_suffix(".pdf"),
    metadata={
        "Title": "RetainZ: mechanism controls",
        "Author": "Minxing Chu and Ruoxi Yang",
        "Subject": "Recorded FEMU measurements. Means and min-max ranges across three inputs. "
        "Cached cost includes summary maintenance; shared admission at 90% load.",
    },
)
fig.savefig(OUT.with_suffix(".png"), dpi=400)
plt.close(fig)
OUT.with_suffix(".data.json").write_text(json.dumps(provenance, indent=2) + "\n")
print(
    f'Created {OUT.with_suffix(".pdf")} ({W} x {H} pt), {len(provenance)} measured groups.'
)
