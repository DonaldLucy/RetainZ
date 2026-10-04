#!/usr/bin/env python3
"""Generate matched placement controls, including delayed and extended retention."""

import argparse
import json
import random
from pathlib import Path
from generate_trace import generate

ZONE = 4194304


def make_trace(profile, seed, path):
    small = profile == "packing"
    load = dict(core=0.84, packing=0.70, pressure=0.90, delayed=0.80, promotion=0.80)[
        profile
    ]
    config = generate(
        path,
        kind="tenants",
        epochs=120,
        ratio=0.8 if small else 3.2,
        zone=ZONE,
        zones=64,
        occupancy=load,
        seed=seed,
    )
    config["profile"] = profile
    if profile in ("delayed", "promotion"):
        rng = random.Random(5000 + seed)
        lines = [
            line.split()
            for line in path.read_text().splitlines()
            if line != "Q" and " resident " not in line
        ]
        creates = {x[2]: x for x in lines if x[0] == "C"}
        selected = []
        if profile == "promotion":
            # Two online extensions, never information made available at birth.
            for epoch in (20, 60):
                selected.append(
                    next(x[2] for x in lines if x[0] == "C" and int(x[1]) == epoch)
                )
            lines = [x for x in lines if not (x[0] == "D" and x[2] in selected)]
            lines += [
                ["P", str(int(creates[key][1]) + 1), key, "inf"] for key in selected
            ]
            config["online_promotions"] = selected
        else:
            for row in lines:
                if row[0] == "D":
                    row[1] = str(int(row[1]) + rng.randrange(4))
            config["deletion_delay_epochs"] = [0, 3]
        lines = [x for x in lines if int(x[1]) <= 120]
        deaths = {x[2]: int(x[1]) for x in lines if x[0] == "D"}
        # Compute occupancy from the actual event lifetimes, including changes.
        peak = max(
            sum(
                int(x[5])
                for key, x in creates.items()
                if int(x[1]) <= epoch < deaths.get(key, 10**12)
            )
            for epoch in range(1, 121)
        )
        resident = max(0, int(((64 - 3) * ZONE * load - peak) / ZONE)) * ZONE
        config["resident_bytes"] = resident
        order = {"C": 0, "P": 1, "D": 2, "R": 3}
        lines.sort(key=lambda x: (int(x[1]), order[x[0]]))
        # Read back every final live generation, including delayed/promoted ones.
        existing = {x[2] for x in lines if x[0] == "R" and x[1] == "120"}
        lines += [
            ["R", "120", key]
            for key in creates
            if key not in deaths and key not in existing
        ]
        if resident:
            lines.insert(0, ["C", "0", "resident", "inf", "extreme", str(resident)])
        path.write_text("\n".join(" ".join(x) for x in lines) + "\nQ\n")
        config["requested_checkpoints"] = len(creates) + bool(resident)
        config["checkpoint_logical_bytes"] = (
            sum(int(x[5]) for x in creates.values()) + resident
        )
    path.with_suffix(".json").write_text(json.dumps(config, indent=2) + "\n")
    return path


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--profile",
        choices=["core", "packing", "pressure", "delayed", "promotion"],
        required=True,
    )
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    make_trace(a.profile, a.seed, a.out)
