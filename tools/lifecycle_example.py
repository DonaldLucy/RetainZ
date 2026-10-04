#!/usr/bin/env python3
"""Execute the introductory allocation illustration; functional, not timing."""

import csv, json, subprocess, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results/lifecycle"
OUT.mkdir(parents=True, exist_ok=True)
TRACE = """C 1 C1 7 medium 49152
C 2 C2 13 medium 32768
C 3 C3 7 short 32768
D 7 C1
D 7 C3
C 8 C4 13 medium 49152
R 8 C2
R 8 C4
Q
"""
(OUT / "trace.tsv").write_text(TRACE)
summary = {}
with tempfile.TemporaryDirectory(prefix="retainz-zenfs-illustration-") as tmp:
    binary = ROOT / "build/retainz"
    for policy in ("zenfs_alloc", "retainz"):
        prefix = OUT / policy
        cmd = [
            str(binary),
            str(Path(tmp) / policy),
            str(OUT / "trace.tsv"),
            policy,
            "81920",
            str(prefix),
            "--file-zones",
            "16",
            "--meta-zones",
            "3",
            "--max-open",
            "4",
            "--max-active",
            "8",
            "--zenfs-resources",
            "--adaptive",
        ]
        if policy == "retainz":
            cmd += ["--pressure-fallback"]
        r = subprocess.run(cmd, capture_output=True, text=True, check=True)
        with prefix.with_suffix(".csv").open() as f:
            rows = list(csv.DictReader(f))
        final = rows[-1]
        summary[policy] = {
            k: int(final[k])
            for k in ("live_bytes", "invalid_bytes", "tail_bytes", "gc_write_bytes")
        }
        summary[policy]["zone_bytes"] = 81920
        assert all(r["verified"] == "1" for r in rows if r["event"] == "R")
assert summary["zenfs_alloc"]["invalid_bytes"] == 32768
assert summary["zenfs_alloc"]["tail_bytes"] == 49152
assert summary["retainz"]["invalid_bytes"] == summary["retainz"]["tail_bytes"] == 0
(OUT / "verified.json").write_text(
    json.dumps(
        dict(
            scope="File-device functional illustration; no device timing claim",
            results=summary,
        ),
        indent=2,
    )
    + "\n"
)
print(json.dumps(summary))
