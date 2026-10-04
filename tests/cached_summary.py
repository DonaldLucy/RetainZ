#!/usr/bin/env python3
"""Compare optimized bookkeeping with the frozen scan-based implementation."""

import argparse, csv, json, random, subprocess, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--reference", default=str(ROOT / "build/history_scan"))
    p.add_argument("--candidate", default=str(ROOT / "build/fixed_reserve"))
    a = p.parse_args()
    checked = []
    fields = [
        "event",
        "epoch",
        "key",
        "status",
        "logical_bytes",
        "payload_bytes",
        "gc_write_bytes",
        "metadata_write_bytes",
        "read_bytes",
        "invalid_bytes",
        "tail_bytes",
        "stranded_tail_bytes",
        "live_bytes",
        "free_bytes",
        "empty_zones",
        "open",
        "active",
        "peak_open",
        "peak_active",
        "peak_live_bytes",
        "peak_reserved_bytes",
        "resets",
        "erase_max",
        "erase_cv",
        "restore_bytes",
        "verified",
        "shards_written",
        "pressure_fallback_allocations",
        "pressure_fallback_bytes",
    ]
    with tempfile.TemporaryDirectory(prefix="retainz-equivalence-") as td:
        folder = Path(td)
        for seed in range(16):
            rng = random.Random(seed)
            lines = []
            live = []
            anchors = set()
            for epoch in range(1, 41):
                key = f"c{epoch}"
                anchor = epoch % 7 == 0
                if anchor:
                    anchors.add(key)
                sizes = [
                    rng.randrange(1, 20) * 4096 + rng.randrange(0, 31)
                    for _ in range(1 + rng.randrange(3))
                ]
                lines.append(
                    f'C {epoch} {key} {"inf" if anchor else epoch+3+rng.randrange(4)} {"extreme" if anchor else "short"} {sum(sizes)} '
                    + ",".join(map(str, sizes))
                )
                live.append(key)
                if epoch % 11 == 0:
                    target = live[-2]
                    anchors.add(target)
                    lines.append(f"P {epoch} {target} inf")
                for old in live[:-3]:
                    if old not in anchors:
                        lines.append(f"D {epoch} {old}")
                live = [x for x in live if x in anchors or x in live[-3:]]
                if epoch % 8 == 0:
                    lines.append(f"R {epoch} {key}")
            trace = folder / "trace.tsv"
            trace.write_text("\n".join(lines + ["Q"]) + "\n")
            for policy in ["fifo", "coarse", "retainz", "zenfs_alloc"]:
                outputs = []
                for label, exe in [
                    ("reference", a.reference),
                    ("candidate", a.candidate),
                ]:
                    base = folder / f"{seed}-{policy}-{label}"
                    args = [
                        exe,
                        str(base) + ".dev",
                        str(trace),
                        policy,
                        "262144",
                        str(base),
                        "--file-zones",
                        "20",
                        "--meta-zones",
                        "3",
                        "--max-open",
                        "4",
                        "--max-active",
                        "8",
                        "--zenfs-resources",
                    ]
                    if policy in ["coarse", "retainz"]:
                        args += ["--pressure-fallback"]
                    if policy == "retainz":
                        args += ["--adaptive"]
                    run = subprocess.run(args, capture_output=True, text=True)
                    events = [
                        {k: v for k, v in json.loads(line).items() if k != "time_ns"}
                        for line in base.with_suffix(".events.jsonl")
                        .read_text()
                        .splitlines()
                    ]
                    rows = [
                        {k: r[k] for k in fields}
                        for r in csv.DictReader(base.with_suffix(".csv").open())
                    ]
                    outputs.append(
                        (
                            run.returncode,
                            events,
                            rows,
                            base.with_suffix(".zones.csv").read_text(),
                        )
                    )
                assert outputs[0] == outputs[1], (seed, policy)
                checked.append(
                    dict(
                        seed=seed,
                        policy=policy,
                        exit=outputs[0][0],
                        events=len(outputs[0][1]),
                    )
                )
    out = ROOT / "results/tests/cached_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            dict(
                all_pass=True,
                traces=len(checked),
                comparison="Exact zone decisions, non-timing counters, final zone states, and completion status against full-history reference implementation",
                cases=checked,
            ),
            indent=2,
        )
        + "\n"
    )
    print(f"Exact placement/accounting equivalence: {len(checked)} traces passed")


if __name__ == "__main__":
    main()
