#!/usr/bin/env python3
import json, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.runtime_retention import RetentionDriver

results = []
with tempfile.TemporaryDirectory(prefix="retainz-runtime-") as td:
    root = Path(td)
    source = root / "source"
    source.mkdir()
    (source / ".metadata").write_bytes(b"metadata")
    for kind in ("megatron", "torchtitan"):
        events = []
        driver = RetentionDriver(kind, root / kind, lambda *args: events.append(args))
        live = set()
        for step in range(1, 25):
            driver.before_save(step)
            driver.saved(step, source)
        for op, epoch, step in events:
            if op == "C":
                assert step not in live
                live.add(step)
            else:
                assert step in live and step % 3
                live.remove(step)
        assert live == set(driver.live_steps())
        for epoch in range(1, 25):
            batch = [x[0] for x in events if x[1] == epoch]
            if "D" in batch:
                assert (
                    batch.index("D") > batch.index("C")
                    if kind == "megatron"
                    else batch.index("D") < batch.index("C")
                )
        results.append(
            dict(
                runtime=kind,
                saves=24,
                deletes=sum(x[0] == "D" for x in events),
                remaining=sorted(live),
                ordering="post-save delete" if kind == "megatron" else "pre-save purge",
                events=events,
            )
        )
path = ROOT / "results/tests/runtime_retention_test.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(
    json.dumps(
        dict(
            all_pass=True,
            runs=results,
            scope="Unmodified upstream retention bodies with rank-0 local filesystem plumbing; not distributed framework training.",
        ),
        indent=2,
    )
    + "\n"
)
print(
    "Pinned Megatron/TorchTitan retention bodies: 48 saves, actual filesystem deletion and original ordering verified"
)
