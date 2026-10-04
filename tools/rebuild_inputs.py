#!/usr/bin/env python3
"""Regenerate non-payload trace bytes and compare them with the declared inputs."""

import json
from pathlib import Path

from generate_controls import make_trace
from generate_trace import generate

ROOT = Path(__file__).resolve().parents[1]


def main():
    cases = json.loads((ROOT / "experiments/paper.json").read_text())
    traces = sorted({c["trace"] for c in cases if not c.get("runtime")})
    for rel in traces:
        archived = ROOT / rel
        config = json.loads(archived.with_suffix(".json").read_text())
        out = (
            ROOT
            / "build/regenerated"
            / archived.relative_to(ROOT / "experiments/traces")
        )
        scale = config.get("byte_scale_factor", 1)
        if config.get("profile"):
            make_trace(config["profile"], config["seed"], out)
        else:
            generate(
                out,
                kind=config["kind"],
                epochs=config["epochs"],
                ratio=config["ratio"],
                zone=config["zone_bytes"],
                zones=config["zones"],
                occupancy=config["occupancy_target"],
                seed=config["seed"],
                k=config["k"],
                retain=config["retain"],
                probability=config["probability"],
                manifest=ROOT / config["manifest"] if config.get("manifest") else None,
            )
        width = config.get("hint_rounding_epochs", 1)
        if scale > 1 or width > 1:
            lines = []
            for line in out.read_text().splitlines():
                f = line.split()
                if f[0] == "C":
                    if f[3] != "inf":
                        f[3] = str((int(f[3]) + width - 1) // width * width)
                    f[5] = str(int(f[5]) * scale)
                    if len(f) == 7:
                        f[6] = ",".join(str(int(n) * scale) for n in f[6].split(","))
                lines.append(" ".join(f))
            out.write_text("\n".join(lines) + "\n")
        if out.read_bytes() != archived.read_bytes():
            raise AssertionError(f"Regenerated input differs: {rel}")
    print(f"{len(traces)} regenerated trace files match the archived input bytes.")


if __name__ == "__main__":
    main()
