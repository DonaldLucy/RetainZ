#!/usr/bin/env python3
"""Checkpoint event traces, never precomputed placement actions.

Synthetic cadence is an explicit admission contract. Irregular promotion and
delayed deletion cases exercise disagreement between hints and actual events.
Each seed changes total sizes/cadence phases, not only shard boundaries.
"""

import argparse, json, random, math
from pathlib import Path


def generate(
    out,
    kind="mixed",
    epochs=120,
    ratio=1.6,
    zone=4194304,
    zones=64,
    occupancy=0,
    seed=0,
    k=3,
    retain=10,
    probability=0.05,
    manifest=None,
):
    rng = random.Random(seed)
    records = []
    metadata = {}
    deletes = {}
    promotes = {}

    def add(e, key, death, size, shards=None, promote=False):
        size = max(4096, round(size / 4096) * 4096)
        if shards is None:
            # Actual input byte counts are fixed before execution. All policies
            # receive precisely this same event trace.
            weights = [rng.lognormvariate(0, 0.5) for _ in range(8)]
            ns = [
                max(4096, int(size * w / sum(weights)) // 4096 * 4096)
                for w in weights[:-1]
            ]
            if sum(ns) >= size:
                ns = [4096] * 7
            shards = ns + [size - sum(ns)]
        else:
            size = sum(shards)
        hint = death
        if promote:
            promotes.setdefault(e + 1, []).append(key)
            death = 10**12
        life = (
            "extreme"
            if hint >= 10**12
            else "short" if hint - e <= 4 else "medium" if hint - e <= 16 else "long"
        )
        records.append((e, key, hint, life, size, shards))
        metadata[key] = {
            "birth": e,
            "death": death,
            "size": sum(math.ceil(n / 4096) * 4096 for n in shards),
        }
        if death < 10**12:
            actual = death + rng.randrange(0, 4) if kind == "delayed" else death
            metadata[key]["death"] = actual
            deletes.setdefault(actual, []).append(key)

    if manifest:
        rows = [json.loads(x) for x in Path(manifest).read_text().splitlines()]
        for i, row in enumerate(rows[:epochs], 1):
            ns = [f["size_bytes"] for f in row["files"]]
            add(i, f"real{i}", 10**12 if i % retain == 0 else i + k, sum(ns), ns)
        epochs = min(epochs, len(rows))
    else:
        for e in range(1, epochs + 1):
            if kind in ("mixed", "tenants"):
                streams = (
                    [(1, 3, 0.5), (2, 4, 0.5)]
                    if kind == "mixed"
                    else [(1, 3, 0.23), (2, 3, 0.37), (3, 4, 0.53), (4, 5, 0.71)]
                )
                for j, (cadence, keep, scale) in enumerate(streams):
                    phase = (seed + j) % cadence
                    if e % cadence == phase:
                        add(
                            e,
                            f"s{j}_{e}",
                            e + cadence * keep,
                            ratio * zone * scale * rng.uniform(0.85, 1.15),
                        )
            elif kind == "multilevel":
                add(e, f"l{e}", e + 3, ratio * zone)
                if e % 6 == 0:
                    add(e, f"g{e}", e + 24, ratio * zone)
                if e % 72 == 0:
                    add(e, f"a{e}", 10**12, ratio * zone)
            else:
                anchor = kind == "periodic" and e % retain == 0
                promotion = kind == "promotion" and rng.random() < probability
                add(
                    e,
                    f"c{e}",
                    10**12 if anchor else e + k,
                    ratio * zone,
                    promote=promotion,
                )
    lines = []
    # Target steady logical occupancy includes the active checkpoint set.
    # Report exact measured occupancy; the target is only trace construction.
    if occupancy:
        livepeak = max(
            sum(q["size"] for q in metadata.values() if q["birth"] <= e < q["death"])
            for e in range(1, epochs + 1)
        )
        resident = (
            max(0, int(((zones - 3) * zone * occupancy - livepeak) / zone)) * zone
        )
        if resident:
            lines.append(f"C 0 resident inf extreme {resident}")
    else:
        resident = 0
    byepoch = {}
    for row in records:
        byepoch.setdefault(row[0], []).append(row)
    for e in range(1, epochs + 1):
        for row in byepoch.get(e, []):
            ep, key, d, cl, size, ns = row
            lines.append(
                f'C {ep} {key} {"inf" if d>=10**12 else d} {cl} {size} '
                + ",".join(map(str, ns))
            )
        for key in promotes.get(e, []):
            lines.append(f"P {e} {key} inf")
        for key in deletes.get(e, []):
            lines.append(f"D {e} {key}")
        if e % 20 == 0 and byepoch.get(e):
            lines.append(f"R {e} {byepoch[e][-1][1]}")
    live = [key for key, q in metadata.items() if q["death"] > epochs]
    for key in live:
        lines.append(f"R {epochs} {key}")
    lines.append("Q")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(lines) + "\n")
    config = dict(
        kind=kind,
        epochs=epochs,
        ratio=ratio,
        zone_bytes=zone,
        zones=zones,
        occupancy_target=occupancy,
        resident_bytes=resident,
        seed=seed,
        k=k,
        retain=retain,
        probability=probability,
        manifest=str(manifest) if manifest else None,
        requested_checkpoints=len(records) + (resident > 0),
        checkpoint_logical_bytes=sum(r[4] for r in records) + resident,
        protocol="C(write+commit) before actual D; P explicit; R live-only",
    )
    Path(out).with_suffix(".json").write_text(json.dumps(config, indent=2))
    return config


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--kind", default="mixed")
    p.add_argument("--epochs", type=int, default=120)
    p.add_argument("--ratio", type=float, default=1.6)
    p.add_argument("--zone", type=int, default=4194304)
    p.add_argument("--zones", type=int, default=64)
    p.add_argument("--occupancy", type=float, default=0)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--k", type=int, default=3)
    p.add_argument("--retain", type=int, default=10)
    p.add_argument("--probability", type=float, default=0.05)
    p.add_argument("--manifest")
    a = p.parse_args()
    print(json.dumps(generate(**vars(a))))
