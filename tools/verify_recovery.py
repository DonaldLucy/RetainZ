#!/usr/bin/env python3
"""Validate recovery records from new FEMU processes on the same namespace."""

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(folder):
    cases = json.loads((ROOT / "experiments/recovery.json").read_text())
    for case in cases:
        out = folder / case["name"]
        status = json.loads((out / "run.json").read_text())
        events = json.loads((out / "recovery.json").read_text())
        assert status["guest_success"] and status["process_exit"] == 0, case["name"]
        assert events["RETAINZ_CRASH_EXIT"] == "86", case["name"]
        assert events["RETAINZ_RESTART_EXIT"] == "0", case["name"]
        assert events["RETAINZ_RECOVERY_EXIT"] == "0", case["name"]
        with (out / "recovered.csv").open() as f:
            rows = list(csv.DictReader(f))
        reads = [r for r in rows if r["event"] == "R"]
        assert reads and all(
            r["status"] == "ok" and r["verified"] == "1" for r in reads
        ), case["name"]
        if "afterfinishjournal" in case["name"]:
            assert int(rows[-1]["stranded_tail_bytes"]) == 2097152
            with (out / "continued.csv").open() as f:
                continued = list(csv.DictReader(f))
            assert any(r["event"] == "C" and r["status"] == "ok" for r in continued)
        elif "aftercommit" not in case["name"]:
            assert events["RETAINZ_UNCOMMITTED_RESTORE_EXIT"] == "1", case["name"]
        print(case["name"] + ": verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/recovery")
    verify(parser.parse_args().input)
