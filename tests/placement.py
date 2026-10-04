#!/usr/bin/env python3
"""Exercise controls, readback and unchanged full-policy decisions on file devices.

File-device durations are never recorded as FEMU performance evidence.
"""

import argparse
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from generate_trace import generate

FIELDS = [
    "event",
    "epoch",
    "key",
    "status",
    "logical_bytes",
    "payload_bytes",
    "gc_write_bytes",
    "metadata_write_bytes",
    "invalid_bytes",
    "tail_bytes",
    "live_bytes",
    "resets",
    "restore_bytes",
    "verified",
]
VARIANTS = [
    "full",
    "exact_epoch",
    "dbt_only",
    "tail_only",
    "no_span",
    "no_density",
    "isolation",
    "zenfs_alloc",
]


def run(binary, folder, label, trace, variant="full", recover=None):
    prefix = folder / label
    policy = variant if variant in ("isolation", "zenfs_alloc") else "retainz"
    command = [
        str(binary),
        str(recover or prefix) + ".device",
        str(trace),
        policy,
        "262144",
        str(prefix),
        "--file-zones",
        "64",
        "--meta-zones",
        "12",
        "--max-open",
        "4",
        "--max-active",
        "8",
        "--zenfs-resources",
        "--borrow-reserve",
        "--adaptive",
    ]
    if policy == "retainz":
        command += ["--pressure-fallback"]
        if variant != "reference":
            command += ["--experiment", variant]
    if recover:
        command += ["--recover"]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (label, proc.stderr)
    with prefix.with_suffix(".csv").open() as f:
        rows = list(csv.DictReader(f))
    assert rows[-1]["event"] == "Q"
    restored = [r for r in rows if r["event"] == "R"]
    assert restored and all(r["verified"] == "1" for r in restored)
    events = [
        {k: v for k, v in json.loads(line).items() if k != "time_ns"}
        for line in prefix.with_suffix(".events.jsonl").read_text().splitlines()
    ]
    return rows, events


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", default=str(ROOT / "build/retainz"))
    parser.add_argument("--reference", default=str(ROOT / "build/placement_reference"))
    args = parser.parse_args()
    checks = []
    with tempfile.TemporaryDirectory(prefix="retainz-placement-") as temp:
        folder = Path(temp)
        # Reachable workloads include different cadence and size regimes,
        # later retention extensions and delayed explicit deletion.
        for kind, ratio in [
            ("tenants", 3.2),
            ("tenants", 0.8),
            ("promotion", 0.8),
            ("delayed", 0.8),
        ]:
            trace = folder / "trace.tsv"
            generate(
                trace,
                kind=kind,
                ratio=ratio,
                epochs=40,
                zone=262144,
                zones=64,
                seed=4,
                occupancy=0.50,
            )
            reference, ref_events = run(
                args.reference, folder, "reference", trace, "reference"
            )
            rows, events = run(args.candidate, folder, "full", trace)
            assert ref_events == events, (kind, ratio, "full changed placement")
            assert [[r[k] for k in FIELDS] for r in reference] == [
                [r[k] for k in FIELDS] for r in rows
            ]
            for variant in VARIANTS[1:]:
                rows, _ = run(args.candidate, folder, variant, trace, variant)
                latest = next(r["key"] for r in reversed(rows) if r["event"] == "C")
                recovery = folder / "recover.tsv"
                recovery.write_text(f"R 41 {latest}\nQ\n")
                run(
                    args.candidate,
                    folder,
                    variant + "-recovered",
                    recovery,
                    variant,
                    folder / variant,
                )
            checks.append(
                dict(
                    kind=kind,
                    ratio=ratio,
                    full_equivalent=True,
                    all_controls_readback=True,
                )
            )
            for file in folder.glob("*.device*"):
                file.unlink()
        # Independently calculated examples distinguish the two score terms.
        examples = [
            (
                "tail_cost",
                "C 1 a 7 medium 65536\nC 1 b 8 medium 65536",
                "dbt_only",
                True,
                False,
            ),
            (
                "dbt_cost",
                "C 1 a 6 medium 196608\nC 2 b 8 medium 32768",
                "tail_only",
                False,
                True,
            ),
            (
                "exact_group",
                "C 1 a 7 medium 65536\nC 1 b 8 medium 65536",
                "exact_epoch",
                True,
                False,
            ),
            (
                "generation",
                "C 1 a 7 medium 65536\nC 1 b 7 medium 65536",
                "isolation",
                True,
                False,
            ),
        ]
        for name, creates, variant, normal_same, control_same in examples:
            trace = folder / "example.tsv"
            trace.write_text(creates + "\nR 3 a\nR 3 b\nQ\n")
            observed = []
            for choice in ("full", variant):
                _, events = run(
                    args.candidate, folder, name + "-" + choice, trace, choice
                )
                writes = {e["key"]: e["zone"] for e in events if e["event"] == "write"}
                observed.append(writes["a"] == writes["b"])
            assert observed == [normal_same, control_same], (name, observed)
            checks.append(
                dict(
                    example=name,
                    full_same_zone=observed[0],
                    control_same_zone=observed[1],
                )
            )
        # A reachable tight-space state isolates the span gate before emergency
        # relaxation/admission. The finite tail is available only without it.
        trace = folder / "span.tsv"
        trace.write_text(
            "C 1 anchor inf extreme 262144\nC 1 a 20 long 196608\n"
            "C 2 b 5 short 65536\nR 3 a\nR 3 b\nR 3 anchor\nQ\n"
        )
        for variant in ("full", "no_span"):
            prefix = folder / ("span-" + variant)
            proc = subprocess.run(
                [
                    args.candidate,
                    str(prefix) + ".device",
                    str(trace),
                    "retainz",
                    "262144",
                    str(prefix),
                    "--file-zones",
                    "6",
                    "--meta-zones",
                    "3",
                    "--adaptive",
                    "--zenfs-resources",
                    "--experiment",
                    variant,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if variant == "full":
                assert proc.returncode == 1 and "ENOSPC:" in proc.stderr
            else:
                assert proc.returncode == 0, proc.stderr
                with prefix.with_suffix(".csv").open() as f:
                    assert all(
                        r["verified"] == "1"
                        for r in csv.DictReader(f)
                        if r["event"] == "R"
                    )
        checks.append(
            dict(example="span_gate_before_emergency_relaxation", passed=True)
        )
        trace = folder / "density.tsv"
        lines = []
        for epoch in range(1, 21):
            lines.append(f"C {epoch} c{epoch} {epoch+2} short 65536")
            if epoch > 2:
                lines.append(f"D {epoch} c{epoch-2}")
            lines.append(f"R {epoch} c{epoch}")
        trace.write_text("\n".join(lines + ["Q"]) + "\n")
        footprint = {}
        for variant in ("full", "no_density"):
            rows, _ = run(args.candidate, folder, "density-" + variant, trace, variant)
            states = {int(r["epoch"]): r for r in rows if r["event"] in ("C", "D")}
            footprint[variant] = (
                sum(
                    int(r["invalid_bytes"]) + int(r["tail_bytes"])
                    for epoch, r in states.items()
                    if epoch > 8
                )
                / 12
                / 262144
            )
        assert footprint == dict(full=0.75, no_density=1), footprint
        checks.append(dict(example="size_adjustment", footprint_zones=footprint))
    output = ROOT / "results/tests/placement.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            dict(all_pass=True, backend="functional file-device only", checks=checks),
            indent=2,
        )
        + "\n"
    )
    print(json.dumps(dict(all_pass=True, workloads=4, variants=8, examples=6)))


if __name__ == "__main__":
    main()
