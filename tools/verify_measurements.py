#!/usr/bin/env python3
"""Cross-check executed online measurements against traces and independent logs.

This validates accounting consistency, not the hardware or the experiment's
statistical interpretation. Device readback and codec checks remain separate.
"""

import argparse
import json
from pathlib import Path
import sys

from measurements import ROOT, local_path, read_trace, rows, jsonl_events


def verify_zenfs(folder, run):
    summary = json.loads((folder / "result.summary.json").read_text())
    data = rows(folder / "result.events.csv")
    states = rows(folder / "result.states.csv")
    _, trace = read_trace(local_path(run.get("input_trace", run["case"]["trace"])))
    actual = [
        r for r in data if r["op"] in ("C", "D", "P", "R") and r["success"] == "1"
    ]
    checks = {
        "successful_trace_prefix": [
            (r["op"], int(r["epoch"]), r["key"]) for r in actual
        ]
        == [(r["event"], r["epoch"], r["key"]) for r in trace[: len(actual)]],
        "upstream_space_accounting": summary["space_accounting_valid"],
    }
    if summary["success"]:
        checks["complete_trace"] = len(actual) == len(trace)
    if run["case"].get("variant") == "zenfs_gc":
        checks["requested_gc_eligible_names"] = (
            summary.get("gc_eligible_shard_names") is True
        )
    checkpoints = {e["key"]: e for e in trace if e["event"] == "C"}
    commits = [r for r in actual if r["op"] == "C"]
    restores = [r for r in actual if r["op"] == "R"]
    checks["committed_count"] = len(commits) == summary["commits"]
    checks["logical_bytes"] = (
        sum(checkpoints[r["key"]]["logical_bytes"] for r in commits)
        == summary["logical_written_bytes"]
    )
    checks["restored_bytes"] = (
        sum(checkpoints[r["key"]]["logical_bytes"] for r in restores)
        == summary["restored_bytes"]
    )
    checks["restore_count"] = len(restores) == summary["restores"]
    # Upstream harness summary uses (whole_run_success && any_restore).
    # A later ENOSPC therefore clears this summary flag even when an earlier
    # R event completed its bytewise verification. Preserve both facts.
    checks["restore_summary_semantics"] = summary["payload_verified"] == bool(
        summary["success"] and restores
    )
    checks["completed_restore_events_verified"] = all(
        r["success"] == "1" for r in data if r["op"] == "R"
    )
    checks["state_byte_conservation"] = all(
        int(r["written_bytes"]) == int(r["live_bytes"]) + int(r["dead_bytes"])
        for r in states
    )
    checks["capacity_bounds"] = all(
        0 <= int(r["live_bytes"]) <= int(r["written_bytes"])
        and int(r["written_bytes"]) + int(r["tail_bytes"])
        <= summary["data_capacity_bytes"]
        for r in states
    )
    for field in ("host_written_bytes", "gc_written_bytes", "resets", "allocator_ns"):
        values = [int(r[field]) for r in states]
        checks[field + "_monotonic"] = all(a <= b for a, b in zip(values, values[1:]))
    checks["open_limit"] = summary["peak_open"] <= run["case"]["open"]
    checks["active_limit"] = summary["peak_active"] <= run["case"]["active"]
    return dict(
        case=folder.name,
        backend="zenfs",
        guest_success=summary["success"],
        checks=checks,
        all_pass=all(checks.values()),
        committed_checkpoints=len(commits),
        restored_checkpoints=len(restores),
    )


def verify(folder):
    run = json.loads((folder / "run.json").read_text())
    if (folder / "result.summary.json").exists() and run["case"]["policy"] == "zenfs":
        return verify_zenfs(folder, run)
    data = rows(folder / "result.csv")
    if not data:
        return None
    if "recovery" in run["case"].get("init", ""):
        return None
    cfg, trace = read_trace(local_path(run.get("input_trace", run["case"]["trace"])))
    if not trace:
        raise ValueError("original input trace is unavailable")
    checks = {}
    last = data[-1]
    ok = bool(run.get("guest_success"))
    actual = [
        r for r in data if r["event"] in ("C", "D", "P", "R") and r["status"] == "ok"
    ]
    checks["successful_trace_prefix"] = [
        (r["event"], int(r["epoch"]), r["key"]) for r in actual
    ] == [(r["event"], r["epoch"], r["key"]) for r in trace[: len(actual)]]
    if ok:
        checks["complete_trace"] = len(actual) == len(trace) and last["event"] == "Q"
    checkpoints = {e["key"]: e for e in trace if e["event"] == "C"}
    committed = [r for r in actual if r["event"] == "C"]
    logical = sum(checkpoints[r["key"]]["logical_bytes"] for r in committed)
    padded = sum(checkpoints[r["key"]]["padded_bytes"] for r in committed)
    checks["logical_bytes"] = int(last["logical_bytes"]) == logical
    checks["committed_payload_bytes"] = (
        int(last["payload_bytes"]) == padded
        if ok
        else int(last["payload_bytes"]) >= padded
    )
    restores = [r for r in actual if r["event"] == "R"]
    expected_restore = sum(checkpoints[r["key"]]["logical_bytes"] for r in restores)
    checks["restore_logical_bytes"] = (
        sum(int(r["restore_bytes"]) for r in restores) == expected_restore
    )
    checks["all_restores_verified"] = all(r["verified"] == "1" for r in restores)
    live = set()
    restore_live = True
    for r in actual:
        if r["event"] == "C":
            live.add(r["key"])
        elif r["event"] == "D":
            live.remove(r["key"])
        elif r["event"] == "R":
            restore_live &= r["key"] in live
    checks["restore_targets_live"] = restore_live
    expected_live = sum(checkpoints[key]["padded_bytes"] for key in live)
    checks["committed_live_bytes"] = (
        int(last["live_bytes"]) == expected_live
        if ok
        else int(last["live_bytes"]) >= expected_live
    )
    for field in [
        "logical_bytes",
        "payload_bytes",
        "gc_write_bytes",
        "metadata_write_bytes",
        "read_bytes",
        "resets",
        "allocator_ns",
    ]:
        values = [int(r[field]) for r in data]
        checks[field + "_monotonic"] = all(a <= b for a, b in zip(values, values[1:]))
    logs, serial_diagnostics = jsonl_events(folder / "result.events.jsonl")
    checks["reset_events"] = sum(r["event"] == "reset" for r in logs) == int(
        last["resets"]
    )
    checks["commit_events"] = sum(r["event"] == "commit" for r in logs) == len(
        committed
    )
    checks["gc_log_bytes"] = sum(r["bytes"] for r in logs if r["event"] == "gc") == int(
        last["gc_write_bytes"]
    )
    checks["payload_log_bytes"] = sum(
        r["bytes"] for r in logs if r["event"] == "write"
    ) == int(last["payload_bytes"])
    zones = rows(folder / "result.zones.csv")
    checks["zone_live_bytes"] = sum(int(r["live_bytes"]) for r in zones) == int(
        last["live_bytes"]
    )
    checks["zone_written_equals_live_plus_invalid"] = sum(
        int(r["written_bytes"]) for r in zones
    ) == int(last["live_bytes"]) + int(last["invalid_bytes"])
    checks["zone_reset_sum"] = sum(int(r["resets"]) for r in zones) == int(
        last["resets"]
    )
    checks["zone_capacity_bounds"] = all(
        0 <= int(r["live_bytes"]) <= int(r["written_bytes"]) <= int(r["capacity"])
        for r in zones
    )
    checks["open_limit"] = max(int(r["peak_open"]) for r in data) <= run["case"]["open"]
    checks["active_limit"] = (
        max(int(r["peak_active"]) for r in data) <= run["case"]["active"]
    )
    reclamation = rows(folder / "result.reclamation.csv")
    deleted = sum(
        checkpoints[r["key"]]["padded_bytes"] for r in actual if r["event"] == "D"
    )
    checks["reclamation_deleted_bytes"] = (
        sum(int(r["bytes"]) for r in reclamation) == deleted
    )
    return dict(
        case=folder.name,
        guest_success=ok,
        checks=checks,
        all_pass=all(checks.values()),
        serial_kernel_messages=serial_diagnostics,
        committed_checkpoints=len(committed),
        restored_checkpoints=len(restores),
        expected_committed_logical_bytes=logical,
        expected_committed_padded_bytes=padded,
        expected_restored_logical_bytes=expected_restore,
        expected_deleted_padded_bytes=deleted,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=ROOT / "data/measurements")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "results/analysis/accounting.json"
    )
    args = parser.parse_args()
    verified = []
    errors = []
    for path in sorted(args.input.glob("*/run.json")):
        try:
            result = verify(path.parent)
            if result:
                verified.append(result)
        except Exception as exc:
            errors.append(dict(case=path.parent.name, error=str(exc)))
    out = dict(
        runs=verified,
        errors=errors,
        all_pass=bool(verified) and not errors and all(r["all_pass"] for r in verified),
        note="Trace/zone/event accounting checks. Successful restore rows separately certify pattern readback; codec preflight covers arbitrary byte patterns. Failed partial checkpoints remain in physical write counters.",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=2) + "\n")
    print(
        json.dumps(
            dict(
                runs=len(verified),
                failed_checks=[r["case"] for r in verified if not r["all_pass"]],
                errors=errors,
            )
        )
    )
    return 0 if out["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
