#!/usr/bin/env python3
"""Normalize executed online/FEMU and ZenFS measurements without imputing data.

Resident preload is excluded from checkpoint latency. Main WA is the difference
between actual cumulative counters after the warmup epoch and the last fully
completed epoch. Whole-run WA and partial-failure traffic remain separate.
Workload seeds are different inputs; plotted bands/ranges are not run CIs.
"""

from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path
import statistics
import re

ROOT = Path(__file__).resolve().parents[1]
POLICY = {
    "fifo": "FIFO",
    "isolation": "Isolation",
    "coarse": "Coarse-4",
    "zenfs_gc": "ZenFS (GC-eligible shard names)",
    "zenfs_alloc": "ZenFS-Alloc",
    "retainz": "RetainZ",
    "fixed2": "Span-2",
    "span2": "Span-2",
    "zenfs": "ZenFS",
    "resource_adaptive": "RetainZ + active pressure",
    "retainz_pressure": "RetainZ + admission",
    "coarse_pressure": "Coarse-4 + admission",
    "nearest": "Nearest expiry",
    "mix_anchors": "Mixed anchors (64-epoch surrogate)",
    **{f"window{w}": f"Window {w}" for w in (1, 2, 4, 8)},
}
COLORS = {
    "FIFO": "#798894",
    "Isolation": "#876B9B",
    "Coarse-4": "#287DA8",
    "Span-2": "#795D9D",
    "RetainZ": "#D87921",
    "ZenFS": "#168374",
    "RetainZ + active pressure": "#168374",
    "RetainZ + admission": "#D87921",
    "Coarse-4 + admission": "#287DA8",
}
ORDER = [
    "FIFO",
    "Isolation",
    "Coarse-4",
    "ZenFS",
    "Span-2",
    "RetainZ",
    "RetainZ + active pressure",
    "Coarse-4 + admission",
    "RetainZ + admission",
]


def num(v, default=None):
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def integer(v, default=0):
    n = num(v)
    return int(n) if n is not None else default


def divide(a, b):
    return a / b if a is not None and b is not None and b > 0 else None


def quantile(values, q):
    if not values:
        return None
    a = sorted(values)
    p = (len(a) - 1) * q
    i = math.floor(p)
    f = p - i
    return a[i] * (1 - f) + a[min(i + 1, len(a) - 1)] * f


def weighted_quantile(values, q):
    """Inverse empirical CDF, with byte weights and no sample interpolation."""
    a = sorted((value, weight) for value, weight in values if weight > 0)
    target = q * sum(weight for _, weight in a)
    cumulative = 0
    for value, weight in a:
        cumulative += weight
        if cumulative >= target:
            return value
    return None


def reclamation_metrics(folder):
    data = rows(folder / "result.reclamation.csv")
    if not data:
        return {}
    observations = [
        dict(
            seconds=integer(r["delay_ns"]) / 1e9,
            epochs=integer(r["delay_epochs"]),
            weight=integer(r["bytes"]),
            censored=integer(r["censored"]) != 0,
        )
        for r in data
    ]
    completed = [r for r in observations if not r["censored"]]
    total = sum(r["weight"] for r in observations)
    reclaimed = sum(r["weight"] for r in completed)
    out = dict(
        reclamation_sample_scope="all deletions in this run; padded-byte weights",
        reclamation_deleted_bytes=total,
        reclamation_reclaimed_bytes=reclaimed,
        reclamation_censored_bytes=total - reclaimed,
        reclamation_censored_byte_fraction=divide(total - reclaimed, total),
    )
    stamped = all(r.get("delete_epoch_stamped") == "1" for r in data)
    out["reclamation_epoch_delay_valid"] = stamped
    for unit in (["seconds", "epochs"] if stamped else ["seconds"]):
        out[f"reclamation_completed_mean_{unit}"] = divide(
            sum(r[unit] * r["weight"] for r in completed), reclaimed
        )
        out[f"reclamation_all_mean_lower_bound_{unit}"] = divide(
            sum(r[unit] * r["weight"] for r in observations), total
        )
        for p in [50, 95, 99]:
            out[f"reclamation_completed_p{p}_{unit}"] = weighted_quantile(
                [(r[unit], r["weight"]) for r in completed], p / 100
            )
            out[f"reclamation_all_p{p}_lower_bound_{unit}"] = weighted_quantile(
                [(r[unit], r["weight"]) for r in observations], p / 100
            )
    return out


def rows(path):
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def save_csv(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(k for r in data for k in r))
    with path.open("w", newline="") as f:
        if fields:
            w = csv.DictWriter(f, fields)
            w.writeheader()
            w.writerows(data)


def local_path(p):
    p = str(p)
    q = Path(p)
    return q if q.is_absolute() else ROOT / q


def read_trace(path):
    meta = {}
    events = []
    if path.with_suffix(".json").exists():
        meta = json.loads(path.with_suffix(".json").read_text())
    if path.exists():
        for line in path.read_text().splitlines():
            p = line.split()
            if not p or p[0].startswith("#") or p[0] == "Q":
                continue
            e = {"event": p[0], "epoch": int(p[1]), "key": p[2]}
            if p[0] == "C":
                e["logical_bytes"] = int(p[5])
                shards = list(map(int, p[6].split(","))) if len(p) > 6 else [int(p[5])]
                e["padded_bytes"] = sum((n + 4095) // 4096 * 4096 for n in shards)
            events.append(e)
    return meta, events


def result_json(folder, console, first=False):
    p = folder / "result.summary.json"
    if p.exists():
        return json.loads(p.read_text())
    found = re.findall(r"^RESULT (\{.*\})\s*$", console, re.M)
    return json.loads(found[0 if first else -1]) if found else {}


def jsonl_events(path):
    """Separate one observed standalone kernel diagnostic from serial JSONL.

    Preserve raw files and report the exact excluded line. Malformed JSON or
    text inserted into a JSON record still fails instead of losing an event.
    """
    events = []
    diagnostics = []
    watchdog = re.compile(
        r"^\[\s*\d+\.\d+\] clocksource: Long readout interval, skipping watchdog check: cs_nsec: \d+ wd_nsec: \d+$"
    )
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        if watchdog.fullmatch(line):
            diagnostics.append(dict(line=number, text=line))
            continue
        events.append(json.loads(line))
    return events, diagnostics


def online_events(folder):
    out = []
    for r in rows(folder / "result.csv"):
        out.append(
            dict(
                event=r["event"],
                epoch=integer(r["epoch"]),
                key=r.get("key", ""),
                success=r.get("status") == "ok",
                seconds=num(r.get("checkpoint_seconds")) if r["event"] == "C" else None,
                logical=integer(r.get("logical_bytes")),
                payload=integer(r.get("payload_bytes")),
                gc=integer(r.get("gc_write_bytes")),
                metadata=integer(r.get("metadata_write_bytes")),
                host=integer(r.get("payload_bytes"))
                + integer(r.get("gc_write_bytes"))
                + integer(r.get("metadata_write_bytes")),
                invalid=num(r.get("invalid_bytes")),
                tail=num(r.get("tail_bytes")),
                stranded=num(r.get("stranded_tail_bytes")),
                live=num(r.get("live_bytes")),
                free=num(r.get("free_bytes")),
                resets=integer(r.get("resets")),
                erase_max=num(r.get("erase_max")),
                erase_cv=num(r.get("erase_cv")),
                allocator_ns=integer(r.get("allocator_ns")),
                write_io_ns=integer(r.get("write_io_ns")),
                gc_ns=integer(r.get("gc_ns")),
                sync_ns=integer(r.get("sync_ns")),
                dbt_byte_seconds=num(r.get("dbt_byte_seconds")),
                restore_io_seconds=num(r.get("restore_io_seconds")),
                verify_seconds=num(r.get("verify_seconds")),
                restore_bytes=integer(r.get("restore_bytes")),
                verified=r.get("verified") == "1",
                peak_rss_bytes=integer(r.get("process_peak_rss_bytes")),
                peak_live_bytes=num(r.get("peak_live_bytes")),
                peak_reserved_bytes=num(r.get("peak_reserved_bytes")),
                peak_open=num(r.get("peak_open")),
                peak_active=num(r.get("peak_active")),
                allocated_payload_estimate_bytes=num(
                    r.get("allocated_payload_estimate_bytes")
                ),
                reserve_borrows=integer(r.get("reserve_borrows")),
                reserve_borrow_remaining_bytes=integer(
                    r.get("reserve_borrow_remaining_bytes")
                ),
                shards_written=integer(r.get("shards_written")),
                pressure_fallback_allocations=integer(
                    r.get("pressure_fallback_allocations")
                ),
                pressure_fallback_bytes=integer(r.get("pressure_fallback_bytes")),
                elapsed_seconds=None,
            )
        )
    return out


def zenfs_events(folder):
    ev = rows(folder / "result.events.csv")
    states = rows(folder / "result.states.csv")
    matching = defaultdict(list)
    for e in ev:
        matching[(integer(e["epoch"]), e["op"], e["key"])].append(e)
    out = []
    logical = 0
    last_epoch = 0
    for r in states:
        ep = integer(r["epoch"])
        op = r["op"]
        key = r["key"]
        if ep >= 0:
            last_epoch = ep
        event = matching[(ep, op, key)].pop(0) if matching[(ep, op, key)] else {}
        if op == "C":
            logical += integer(event.get("logical_bytes"))
        out.append(
            dict(
                event=op,
                epoch=ep if ep >= 0 else last_epoch,
                key=key,
                success=True,
                seconds=num(event.get("seconds")),
                logical=logical,
                payload=None,
                metadata=None,
                gc=integer(r.get("gc_written_bytes")),
                host=integer(r.get("host_written_bytes")),
                invalid=num(r.get("dead_bytes")),
                tail=num(r.get("tail_bytes")),
                stranded=num(r.get("finished_tail_bytes")),
                live=num(r.get("live_bytes")),
                free=None,
                resets=integer(r.get("resets")),
                allocator_ns=integer(r.get("allocator_ns")),
                write_io_ns=integer(r.get("write_io_ns")),
                gc_ns=None,
                sync_ns=None,
                dbt_byte_seconds=None,
                restore_io_seconds=None,
                verify_seconds=None,
                restore_bytes=integer(event.get("logical_bytes")) if op == "R" else 0,
                verified=op == "R" and event.get("success") == "1",
                elapsed_seconds=num(r.get("elapsed_seconds")),
                metadata_sync_ns=integer(r.get("metadata_sync_ns")),
                device_flush_seconds=num(r.get("device_flush_seconds")),
            )
        )
    return out


def zero_counters():
    return {
        k: 0
        for k in [
            "logical",
            "host",
            "gc",
            "payload",
            "metadata",
            "allocator_ns",
            "dbt_byte_seconds",
        ]
    }


def delta(end, begin, k):
    a, b = end.get(k), begin.get(k, 0)
    return a - b if a is not None and b is not None else None


def signature(r, omit=()):
    keys = [
        "kind",
        "backing",
        "capacity_mib",
        "zone_bytes",
        "max_open",
        "max_active",
        "ratio",
        "occupancy_target",
        "k",
        "retain",
        "cadence_ms",
        "study_role",
        "replicate",
    ]
    return tuple((k, r.get(k)) for k in keys if k not in omit)


def parse_run(folder, warmup):
    folder = Path(folder).resolve()
    source_folder = folder.relative_to(ROOT) if folder.is_relative_to(ROOT) else folder
    run = json.loads((folder / "run.json").read_text())
    case = run["case"]
    console = (
        (folder / "console.log").read_text(errors="replace")
        if (folder / "console.log").exists()
        else ""
    )
    if "recovery" in case.get("init", "") or "retainz_fault=" in case.get(
        "kernel_extra", ""
    ):
        return (
            None,
            [],
            [],
            dict(
                case=case.get("name", folder.name),
                kind="process_recovery",
                verified="RETAINZ_RECOVERY_EXIT=0" in console,
                crash_exit_86="RETAINZ_CRASH_EXIT=86" in console,
                restart_exit_0="RETAINZ_RESTART_EXIT=0" in console,
            ),
        )
    summ = result_json(folder, console, first=bool(case.get("payload_image")))
    events = (
        zenfs_events(folder)
        if (folder / "result.states.csv").exists()
        else online_events(folder)
    )
    if not events:
        if not run.get("guest_success"):
            return (
                None,
                [],
                [],
                dict(
                    case=case.get("name", folder.name),
                    kind="launch_failure",
                    verified=False,
                    process_exit=run.get("process_exit"),
                    guest_started="RETAINZ_GUEST_START" in console,
                    diagnostic=next(
                        (x for x in console.splitlines() if x.strip()),
                        "no console output",
                    ),
                ),
            )
        raise ValueError("successful guest has no extracted measurement CSV")
    trace = local_path(run.get("input_trace", case.get("trace", "")))
    cfg, expected = read_trace(trace)
    effective_policy = case.get("variant", case["policy"])
    label = POLICY.get(effective_policy, effective_policy)
    success = bool(run.get("guest_success")) and summ.get(
        "success", summ.get("status", "ok") == "ok"
    )
    requested = integer(
        cfg.get("requested_checkpoints"), sum(e["event"] == "C" for e in expected)
    )
    workload_requested = sum(
        e["event"] == "C" and e["key"] != "resident" for e in expected
    )
    if not workload_requested:
        workload_requested = max(0, requested - int(bool(cfg.get("resident_bytes"))))
    completed = [e for e in events if e["event"] == "C" and e["success"]]
    real_commits = [e for e in completed if e["key"] != "resident"]
    reported_commits = integer(
        summ.get("commits", summ.get("committed_checkpoints")), len(completed)
    )
    fail_epoch = integer(summ.get("failed_epoch"))
    if not success and not fail_epoch:
        # A failed ZenFS operation has no state row. The completed event prefix
        # identifies the next expected operation when the original trace exists.
        observed = Counter(
            (e["event"], e["epoch"], e["key"])
            for e in events
            if e["event"] in "CDPR" and e["success"]
        )
        for e in expected:
            key = (e["event"], e["epoch"], e["key"])
            if observed[key]:
                observed[key] -= 1
            else:
                fail_epoch = e["epoch"]
                break
    horizon = integer(cfg.get("epochs"), max(e["epoch"] for e in events))
    if not success:
        horizon = min(horizon, max(0, fail_epoch - 1))
    byepoch = {}
    for e in events:
        if e["success"] and e["event"] in ("C", "D", "P") and e["epoch"] <= horizon:
            byepoch[e["epoch"]] = e
    baseline = zero_counters()
    for e in events:
        if e["event"] == "C" and e["key"] == "resident" and e["success"]:
            baseline = e
    carried = baseline
    epoch_rows = []
    for ep in range(1, horizon + 1):
        carried = byepoch.get(ep, carried)
        if carried.get("invalid") is not None:
            epoch_rows.append(
                dict(
                    case=folder.name,
                    policy=label,
                    epoch=ep,
                    invalid_bytes=carried["invalid"],
                    tail_bytes=carried["tail"],
                    stranded_tail_bytes=carried["stranded"],
                    live_bytes=carried["live"],
                    logical_bytes=carried["logical"],
                    host_written_bytes=carried["host"],
                    gc_written_bytes=carried["gc"],
                    metadata_written_bytes=carried.get("metadata"),
                    resets=carried["resets"],
                )
            )
    end = byepoch[max(byepoch)] if byepoch else baseline
    warm = baseline
    for ep in sorted(byepoch):
        if ep <= warmup:
            warm = byepoch[ep]
    final = events[-1]
    if case["policy"] == "zenfs":
        final = dict(
            final,
            host=integer(summ.get("host_written_bytes"), final["host"]),
            gc=integer(summ.get("gc_written_bytes"), final["gc"]),
            logical=integer(summ.get("logical_written_bytes"), final["logical"]),
            resets=integer(summ.get("data_zone_resets"), final["resets"]),
            allocator_ns=integer(summ.get("allocator_ns"), final["allocator_ns"]),
        )
    latency = [e["seconds"] for e in real_commits if e["seconds"] is not None]
    postlat = [
        e["seconds"]
        for e in real_commits
        if e["epoch"] > warmup and e["epoch"] <= horizon and e["seconds"] is not None
    ]
    zone_rows = rows(folder / "result.zones.csv")
    cap = integer(summ.get("data_capacity_bytes")) or (
        sum(integer(z["capacity"]) for z in zone_rows) if zone_rows else None
    )
    restore = [
        e
        for e in events
        if e["event"] == "R"
        and e["success"]
        and e["verified"]
        and e["key"] != "resident"
    ]
    restore_io = sum(e.get("restore_io_seconds") or 0 for e in restore)
    verify_s = sum(e.get("verify_seconds") or 0 for e in restore)
    if case["policy"] == "zenfs" and not any(
        e["event"] == "R" and e["key"] == "resident" for e in events
    ):
        restore_io = num(summ.get("restore_io_seconds"), 0)
        verify_s = num(summ.get("verify_seconds"), 0)
    wlogical = delta(end, warm, "logical")
    whost = delta(end, warm, "host")
    wgc = delta(end, warm, "gc")
    valid_epochs = [e for e in epoch_rows if e["epoch"] > warmup]

    def avg(field):
        return statistics.mean(e[field] for e in valid_epochs) if valid_epochs else None

    role = case["study"]
    result = dict(
        case=folder.name,
        policy=label,
        raw_policy=effective_policy,
        backing=case.get("backing", "dram"),
        kind=cfg.get("kind", case.get("kind", "unspecified")),
        cadence_ms=case.get("cadence_ms", 0),
        study_role=role,
        replicate=case.get("replicate"),
        seed=cfg.get("seed", case.get("seed")),
        capacity_mib=case.get("capacity_mib"),
        zone_bytes=case.get("zone_bytes"),
        max_open=case.get("open"),
        max_active=case.get("active"),
        ratio=cfg.get("ratio"),
        occupancy_target=cfg.get("occupancy_target"),
        k=cfg.get("k"),
        retain=cfg.get("retain"),
        source_trace=run.get("input_trace", case["trace"]),
        source_csv=(
            str(source_folder / "result.csv")
            if case["policy"] != "zenfs"
            else str(folder / "result.states.csv")
        ),
        guest_success=success,
        process_exit=run.get("process_exit"),
        requested_checkpoints=requested or None,
        requested_workload_checkpoints=workload_requested or None,
        committed_checkpoints=reported_commits,
        committed_workload_checkpoints=len(real_commits),
        completed_fraction=divide(len(real_commits), workload_requested),
        failed_epoch=fail_epoch or None,
        completed_horizon=horizon,
        warmup_epochs=warmup,
        preload_bytes=cfg.get("resident_bytes", baseline.get("logical", 0)),
        data_capacity_bytes=cap,
        logical_bytes_total=final["logical"],
        host_written_bytes_total=final["host"],
        gc_written_bytes_total=final["gc"],
        payload_written_bytes_total=final.get("payload"),
        metadata_written_bytes_total=final.get("metadata"),
        total_host_wa=divide(final["host"], final["logical"]),
        total_gc_wa=divide(final["gc"], final["logical"]),
        preload_excluded_host_wa=divide(
            delta(final, baseline, "host"), delta(final, baseline, "logical")
        ),
        warmup_logical_bytes=wlogical,
        warmup_host_written_bytes=whost,
        warmup_gc_written_bytes=wgc,
        warmup_host_wa=divide(whost, wlogical) if horizon > warmup else None,
        warmup_gc_wa=divide(wgc, wlogical) if horizon > warmup else None,
        warmup_host_wa_until_stop=(
            divide(delta(final, warm, "host"), delta(final, warm, "logical"))
            if horizon > warmup
            else None
        ),
        mean_invalid_bytes=avg("invalid_bytes"),
        mean_tail_bytes=avg("tail_bytes"),
        mean_stranded_tail_bytes=avg("stranded_tail_bytes"),
        mean_live_bytes=avg("live_bytes"),
        mean_live_occupancy=divide(avg("live_bytes"), cap),
        full_trace_mean_live_occupancy=(
            divide(statistics.mean(e["live_bytes"] for e in epoch_rows), cap)
            if epoch_rows
            else None
        ),
        full_trace_mean_reserved_overhead_bytes=(
            statistics.mean(e["invalid_bytes"] + e["tail_bytes"] for e in epoch_rows)
            if epoch_rows
            else None
        ),
        measured_peak_live_occupancy=divide(
            final.get("peak_live_bytes")
            or max((e.get("live") or 0 for e in events), default=0),
            cap,
        ),
        mean_reserved_overhead_bytes=(
            (avg("invalid_bytes") + avg("tail_bytes")) if valid_epochs else None
        ),
        dbt_byte_epochs=(
            sum(e["invalid_bytes"] for e in valid_epochs) if valid_epochs else None
        ),
        invalid_plus_tail_byte_epochs=(
            sum(e["invalid_bytes"] + e["tail_bytes"] for e in valid_epochs)
            if valid_epochs
            else None
        ),
        checkpoint_samples=len(latency),
        checkpoint_mean_seconds=statistics.mean(latency) if latency else None,
        checkpoint_p50_seconds=quantile(latency, 0.5),
        checkpoint_p95_seconds=quantile(latency, 0.95),
        checkpoint_p99_seconds=quantile(latency, 0.99),
        postwarmup_checkpoint_samples=len(postlat),
        postwarmup_checkpoint_mean_seconds=(
            statistics.mean(postlat) if postlat else None
        ),
        postwarmup_checkpoint_p50_seconds=quantile(postlat, 0.5),
        postwarmup_checkpoint_p95_seconds=quantile(postlat, 0.95),
        postwarmup_checkpoint_p99_seconds=quantile(postlat, 0.99),
        allocator_ns_total=final.get("allocator_ns"),
        warmup_allocator_ns=delta(end, warm, "allocator_ns"),
        pressure_fallback_allocations=final.get("pressure_fallback_allocations"),
        pressure_fallback_bytes=final.get("pressure_fallback_bytes"),
        data_zone_resets=final.get("resets"),
        data_zone_reset_max=final.get("erase_max", num(summ.get("erase_max"))),
        data_zone_reset_cv=final.get("erase_cv", num(summ.get("erase_cv"))),
        peak_open_zones=final.get("peak_open", num(summ.get("peak_open"))),
        peak_active_zones=final.get("peak_active", num(summ.get("peak_active"))),
        peak_live_bytes=final.get("peak_live_bytes"),
        peak_reserved_bytes=final.get("peak_reserved_bytes"),
        allocated_payload_estimate_bytes=final.get("allocated_payload_estimate_bytes"),
        reserve_borrows=final.get("reserve_borrows"),
        reserve_borrow_remaining_bytes=final.get("reserve_borrow_remaining_bytes"),
        restore_operations=len(restore),
        restored_logical_bytes=sum(e["restore_bytes"] for e in restore),
        restore_io_seconds=restore_io,
        restore_verify_seconds=verify_s,
        restore_logical_mib_per_second=divide(
            sum(e["restore_bytes"] for e in restore) / 2**20, restore_io
        ),
        process_peak_rss_bytes=max(
            (e.get("peak_rss_bytes", 0) for e in events), default=0
        )
        or integer(summ.get("peak_rss_kib")) * 1024,
        host_qemu_peak_rss_bytes=None,
        walltime_dbt_byte_seconds=None,
        walltime_dbt_method=None,
        codec_verified=(
            "FEMU_CODEC_RESULT " in console and '"verified":true' in console
        ),
        restore_after_enospc_verified=run.get("recovery_after_capacity"),
        input_seed_interpretation="different workload inputs; not identical-workload repeats",
    )
    result.update(reclamation_metrics(folder))
    rss = re.search(r"Maximum resident set size \(kbytes\): (\d+)", console)
    if rss:
        result["host_qemu_peak_rss_bytes"] = int(rss.group(1)) * 1024
    if case["policy"] != "zenfs":
        result["walltime_dbt_byte_seconds"] = (
            delta(end, warm, "dbt_byte_seconds") if horizon > warmup else None
        )
        result["walltime_dbt_method"] = (
            "event-driven counter, includes FEMU and application control time"
        )
    elif horizon > warmup:
        # ZenFS snapshots are discrete samples, so this is deliberately labelled
        # a sample integral, not an exact reset-event timestamp reconstruction.
        selected = [
            e
            for e in events
            if e["epoch"] >= warmup
            and e["epoch"] <= horizon
            and e["elapsed_seconds"] is not None
        ]
        result["walltime_dbt_byte_seconds"] = sum(
            max(0, b["elapsed_seconds"] - a["elapsed_seconds"]) * a["invalid"]
            for a, b in zip(selected, selected[1:])
        )
        result["walltime_dbt_method"] = (
            "left-hold snapshot integral; asynchronous reset times are not captured"
        )
    event_rows = []
    previous = zero_counters()
    for e in events:
        event_rows.append(
            dict(
                case=folder.name,
                policy=label,
                backing=result["backing"],
                event=e["event"],
                epoch=e["epoch"],
                key=e["key"],
                success=e["success"],
                checkpoint_seconds=e["seconds"] if e["event"] == "C" else None,
                allocator_ns_since_previous_event=delta(e, previous, "allocator_ns"),
                resident=e["key"] == "resident",
                after_warmup=e["epoch"] > warmup,
                **{
                    k: e.get(k)
                    for k in [
                        "host",
                        "gc",
                        "logical",
                        "invalid",
                        "tail",
                        "stranded",
                        "live",
                    ]
                },
            )
        )
        previous = e
    for e in epoch_rows:
        e["data_capacity_bytes"] = cap
        e["backing"] = result["backing"]
        e["after_warmup"] = e["epoch"] > warmup
        e["postwarmup_host_wa"] = (
            divide(
                e["host_written_bytes"] - warm.get("host", 0),
                e["logical_bytes"] - warm.get("logical", 0),
            )
            if e["epoch"] > warmup
            else None
        )
        e["postwarmup_gc_wa"] = (
            divide(
                e["gc_written_bytes"] - warm.get("gc", 0),
                e["logical_bytes"] - warm.get("logical", 0),
            )
            if e["epoch"] > warmup
            else None
        )
    return result, epoch_rows, event_rows, None
