#!/usr/bin/env python3
"""Fetch the pinned source bodies and licenses used by the retention driver."""

from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    (
        "megatron",
        "NVIDIA/Megatron-LM",
        "d113016bb2850c4b7d804f24ba72c055d7d4f861",
        [
            ("megatron/training/checkpointing.py", "checkpointing.py"),
            ("LICENSE", "LICENSE"),
        ],
    ),
    (
        "torchtitan",
        "pytorch/torchtitan",
        "35ceeb5901e6ffe6a0a3ecd4c4e6e9e80cbc853e",
        [
            ("torchtitan/components/checkpointer/base.py", "base.py"),
            ("LICENSE", "LICENSE"),
        ],
    ),
]

if __name__ == "__main__":
    for name, repo, revision, files in SOURCES:
        dest = ROOT / "third_party" / name
        dest.mkdir(parents=True, exist_ok=True)
        for remote, local in files:
            url = f"https://raw.githubusercontent.com/{repo}/{revision}/{remote}"
            with urlopen(url, timeout=60) as response:
                data = response.read()
            (dest / local).write_bytes(data)
        print(f"{name}: {revision}")
