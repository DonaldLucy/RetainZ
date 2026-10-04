#!/usr/bin/env python3
"""Recompute paper figures, table cells and claims from recorded measurements."""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

from measurements import ROOT, jsonl_events, parse_run, rows
from verify_measurements import verify

LABELS = {
    "zenfs_alloc": "ZenFS-Alloc",
    "retainz_pressure": "RetainZ",
    "full": "RetainZ",
    "exact_epoch": "Equal-epoch best-fit",
    "dbt_only": "DBT only",
    "tail_only": "Tail only",
    "no_span": "No span constraint",
    "no_density": "No size adjustment",
    "isolation": "Generation isolation",
}
METRICS = ("invalid_zones", "tail_zones", "reserved_zones", "host_wa", "p99_ms")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--measurements", type=Path, default=ROOT / "data/measurements")
    parser.add_argument("--output", type=Path, default=ROOT / "results/analysis")
    parser.add_argument(
        "--check-paper",
        action="store_true",
        help="Check the archived observations against published rounded values.",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cases = json.loads((ROOT / "experiments/paper.json").read_text())
    records, checks, by_name, failures = [], [], {}, []
    for case in cases:
        folder = args.measurements / case["name"]
        if not (folder / "run.json").is_file():
            failures.append({"case": case["name"], "error": "missing run"})
            continue
        try:
            status = json.loads((folder / "run.json").read_text())
            assert (
                status["case"] == case
            ), "configuration differs from experiment manifest"
            record, _, _, diagnostic = parse_run(folder, case["warmup_epochs"])
            assert record is not None, diagnostic
            check = verify(folder)
            assert check["all_pass"], check
            assert record["peak_open_zones"] <= case["open"] - 2
            assert record["peak_active_zones"] <= case["active"] - 1
            if not record["guest_success"]:
                assert status["stop_reason"] == "capacity"
                assert status["recovery_after_capacity"]
                restored = [
                    r for r in rows(folder / "failure.csv") if r["event"] == "R"
                ]
                assert restored and all(r["verified"] == "1" for r in restored)
            record.update(
                study_role=case["study"],
                variant=case.get("experiment", case["policy"]),
                policy=LABELS[case["policy"]],
                hint_rounding_epochs=case.get("hint_rounding_epochs"),
                runtime=case.get("runtime"),
            )
            for target, source, multiplier in [
                ("invalid_zones", "mean_invalid_bytes", 1 / case["zone_bytes"]),
                ("tail_zones", "mean_tail_bytes", 1 / case["zone_bytes"]),
                (
                    "reserved_zones",
                    "mean_reserved_overhead_bytes",
                    1 / case["zone_bytes"],
                ),
                ("host_wa", "warmup_host_wa", 1),
                ("p99_ms", "postwarmup_checkpoint_p99_seconds", 1000),
            ]:
                record[target] = (
                    record[source] * multiplier
                    if record["guest_success"] and record[source] is not None
                    else None
                )
            if case.get("runtime"):
                tensor = json.loads((folder / "tensor_verification.json").read_text())
                assert all(
                    tensor[k]
                    for k in (
                        "bytewise_equal",
                        "model_tensors_equal",
                        "optimizer_tensors_equal",
                        "rng_equal",
                        "next_step_loss_equal",
                        "next_step_parameters_equal",
                        "next_step_optimizer_equal",
                    )
                )
                readback = [
                    r
                    for r in rows(folder / "payload_recovery.csv")
                    if r["event"] == "R"
                ]
                assert len(readback) == len(tensor["live_steps"]) and all(
                    r["verified"] == "1" for r in readback
                )
                record["tensor_verification"] = tensor
            if case["study"] == "metadata":
                probe = json.loads((folder / "metadata.json").read_text())
                assert probe["blocks"] >= 2 * probe["generation_records"] + 3
                record["metadata"] = probe
            records.append(record)
            checks.append(check)
            by_name[case["name"]] = record
        except Exception as error:
            failures.append({"case": case["name"], "error": str(error)})

    def save(name, obj):
        (args.output / (name + ".json")).write_text(json.dumps(obj, indent=2) + "\n")

    save(
        "verification",
        {"configured": len(cases), "verified": len(records), "errors": failures},
    )
    if failures:
        raise SystemExit(json.dumps(failures[:10], indent=2))

    def select(study, **fields):
        return [
            r
            for r in records
            if r["study_role"] == study
            and all(r.get(k) == v for k, v in fields.items())
        ]

    def avg(group, metric):
        assert group and all(
            r["guest_success"] and r[metric] is not None for r in group
        ), metric
        return statistics.mean(r[metric] for r in group)

    summary = []
    groups = defaultdict(list)
    for record in records:
        groups[record["study_role"], record["variant"]].append(record)
    for (study, variant), group in groups.items():
        item = dict(
            study=study,
            variant=variant,
            runs=len(group),
            completed=sum(r["guest_success"] for r in group),
            cases=[r["case"] for r in group],
        )
        for metric in METRICS:
            values = [r[metric] for r in group]
            item[metric] = (
                dict(mean=statistics.mean(values), min=min(values), max=max(values))
                if all(v is not None for v in values)
                else None
            )
        summary.append(item)

    pressure = []
    for load in (70, 80, 82, 84, 86, 88, 90):
        row = dict(target=load)
        for policy, side in [
            ("zenfs_alloc", "baseline"),
            ("retainz_pressure", "method"),
        ]:
            group = select("pressure", raw_policy=policy, occupancy_target=load / 100)
            assert len(group) == 3
            row[side + "_completed"] = [
                r["committed_workload_checkpoints"] for r in group
            ]
            for metric, key in [
                ("reserved_zones", "mean_invalid_plus_tail_zones"),
                ("host_wa", "host_wa"),
                ("p99_ms", "checkpoint_p99_ms"),
            ]:
                row["full_" + side + "_" + key] = (
                    avg(group, metric)
                    if all(r["guest_success"] for r in group)
                    else None
                )
        pressure.append(row)

    def semantic_events(record):
        events, _ = jsonl_events(
            args.measurements / record["case"] / "result.events.jsonl"
        )
        return [{k: v for k, v in event.items() if k != "time_ns"} for event in events]

    cache = []
    for scan in select("allocation_scan"):
        match = dict(seed=scan["seed"], occupancy_target=scan["occupancy_target"])
        (cached,) = select("pressure", raw_policy="retainz_pressure", **match)
        (accounted,) = select("allocation_cached", **match)
        assert (
            semantic_events(scan)
            == semantic_events(cached)
            == semantic_events(accounted)
        )
        data = rows(args.measurements / accounted["case"] / "result.csv")
        warm = [r for r in data if int(r["epoch"]) <= 20][-1]
        last = data[-1]
        n = accounted["postwarmup_checkpoint_samples"]
        selector = (int(last["allocator_ns"]) - int(warm["allocator_ns"])) / n / 1e6
        maintenance = (
            (int(last["hint_update_ns"]) - int(warm["hint_update_ns"])) / n / 1e6
        )
        cache.append(
            dict(
                target=round(100 * scan["occupancy_target"]),
                seed=scan["seed"],
                reference=scan["case"],
                candidate=cached["case"],
                accounted_case=accounted["case"],
                placement_events_equal=True,
                scan_allocator_ms=scan["warmup_allocator_ns"]
                / scan["postwarmup_checkpoint_samples"]
                / 1e6,
                cached_allocator_ms=selector + maintenance,
                accounted_selector_ms=selector,
                summary_update_ms=maintenance,
            )
        )

    hints = []
    for seed in range(3):
        for width in (1, 4, 16):
            group = (
                select(
                    "pressure",
                    raw_policy="retainz_pressure",
                    seed=seed,
                    occupancy_target=0.84,
                )
                if width == 1
                else select("hint_precision", seed=seed, hint_rounding_epochs=width)
            )
            (record,) = group
            hints.append(
                dict(
                    seed=seed,
                    width=width,
                    case=record["case"],
                    complete=record["guest_success"],
                    overhead_zones=record["reserved_zones"],
                )
            )

    claims = []

    def claim(name, value, published, digits, inputs):
        matches = f"{value:.{digits}f}" == f"{published:.{digits}f}"
        claims.append(
            dict(
                claim=name,
                measured=value,
                published=published,
                decimals=digits,
                matches=matches,
                inputs=[r["case"] for r in inputs],
            )
        )

    baseline = select("pressure", raw_policy="zenfs_alloc", occupancy_target=0.84)
    method = select("pressure", raw_policy="retainz_pressure", occupancy_target=0.84)
    for metric, base_value, new_value, digits in [
        ("reserved_zones", 6.53, 3.80, 2),
        ("host_wa", 1.165, 1.003, 3),
        ("p99_ms", 85.3, 44.8, 1),
    ]:
        claim(
            "pressure84.zenfs." + metric,
            avg(baseline, metric),
            base_value,
            digits,
            baseline,
        )
        claim(
            "pressure84.retainz." + metric,
            avg(method, metric),
            new_value,
            digits,
            method,
        )
    claim(
        "abstract.capacity_reduction_percent",
        100 * (1 - avg(method, "reserved_zones") / avg(baseline, "reserved_zones")),
        41.7,
        1,
        baseline + method,
    )
    claim(
        "abstract.p99_reduction_percent",
        100 * (1 - avg(method, "p99_ms") / avg(baseline, "p99_ms")),
        47.5,
        1,
        baseline + method,
    )
    for policy, value in [("zenfs_alloc", 1.514), ("retainz_pressure", 1.214)]:
        group = select("replacement_pressure", raw_policy=policy)
        claim("common_admission90." + policy, avg(group, "host_wa"), value, 3, group)
    for width, value in [(1, 3.80), (4, 4.53), (16, 4.99)]:
        group = [r for r in hints if r["width"] == width]
        claim(
            "hint_precision." + str(width),
            statistics.mean(r["overhead_zones"] for r in group),
            value,
            2,
            group,
        )
    for key, value in [("scan", 5.996), ("cached", 0.270)]:
        group = [r for r in cache if r["target"] == 70]
        claim(
            "allocation_ms." + key,
            statistics.mean(r[key + "_allocator_ms"] for r in group),
            value,
            3,
            [
                by_name[r["reference" if key == "scan" else "accounted_case"]]
                for r in group
            ],
        )
    for record in select("metadata"):
        claim(
            "metadata_kib." + record["raw_policy"],
            record["metadata"]["allocator_usable_bytes"] / 1024,
            430.2,
            1,
            [record],
        )
    full = select("placement", variant="full")
    equal = select("placement", variant="exact_epoch")
    claim(
        "equal_epoch_capacity_reduction_percent",
        100 * (1 - avg(full, "reserved_zones") / avg(equal, "reserved_zones")),
        34.1,
        1,
        full + equal,
    )
    full = select("small_checkpoints", variant="full")
    plain = select("small_checkpoints", variant="no_density")
    claim(
        "size_adaptation_reduction_percent",
        100 * (1 - avg(full, "reserved_zones") / avg(plain, "reserved_zones")),
        11.5,
        1,
        full + plain,
    )
    full = select("span_pressure", variant="full")
    plain = select("span_pressure", variant="no_span")
    claim(
        "span_wa_reduction_percent",
        100 * (1 - avg(full, "host_wa") / avg(plain, "host_wa")),
        4.9,
        1,
        full + plain,
    )
    full = select("zone_capacity_64m", variant="full")
    plain = select("zone_capacity_64m", variant="zenfs_alloc")
    for metric, value in [("reserved_zones", 43.4), ("p99_ms", 38.2)]:
        claim(
            "zone64m." + metric + "_reduction_percent",
            100 * (1 - avg(full, metric) / avg(plain, metric)),
            value,
            1,
            full + plain,
        )

    table = []
    metrics = ("invalid_zones", "tail_zones", "host_wa", "p99_ms")
    digits = (2, 2, 3, 1)

    def table_row(label, study, variants, expected):
        formatted = []
        for metric, precision in zip(metrics, digits):
            values = []
            for variant in variants:
                group = select(study, variant=variant)
                value = avg(group, metric)
                values.append(f"{value:.{precision}f}")
            formatted.append("/".join(values))
        table.append(
            dict(
                label=label,
                study=study,
                variants=variants,
                values=formatted,
                published=expected,
                matches=formatted == expected,
            )
        )

    for variant, expected in [
        ("zenfs_alloc", ["3.66", "3.11", "1.153", "79.8"]),
        ("exact_epoch", ["2.96", "2.82", "1.045", "63.4"]),
        ("dbt_only", ["1.61", "3.61", "1.014", "48.4"]),
        ("tail_only", ["2.35", "2.10", "1.007", "46.9"]),
        ("no_span", ["1.69", "1.88", "1.003", "43.9"]),
        ("full", ["1.11", "2.70", "1.003", "43.6"]),
    ]:
        table_row(LABELS[variant], "placement", [variant], expected)
    for label, study, expected in [
        (
            "90% load",
            "span_pressure",
            ["3.54/3.95", "1.00/0.64", "1.189/1.251", "119.5/127.7"],
        ),
        (
            "Small checkpoints",
            "span_small_checkpoints",
            ["0.81/1.83", "2.04/1.64", "1.008/1.008", "16.8/15.8"],
        ),
        (
            "Delayed deletion",
            "span_delayed_deletion",
            ["1.41/1.89", "2.99/2.36", "1.003/1.003", "45.7/39.8"],
        ),
        (
            "Extended retention",
            "span_extended_retention",
            ["2.17/3.05", "3.30/2.18", "1.003/1.004", "42.2/44.4"],
        ),
    ]:
        table_row(label, study, ["full", "no_span"], expected)
    for label, study, expected in [
        (
            "4 MiB, 6 active",
            "zone_reference",
            ["1.06/3.68", "2.79/3.13", "1.003/1.123", "40.3/72.5"],
        ),
        (
            "4 MiB, 14 active",
            "active_budget_14",
            ["1.11/3.99", "2.75/4.85", "1.003/1.644", "40.7/158.1"],
        ),
        (
            "4 MiB, 30 active",
            "active_budget_30",
            ["1.11/6.34", "2.75/3.66", "1.003/2.080", "37.9/202.7"],
        ),
        (
            "64 MiB, 6 active",
            "zone_capacity_64m",
            ["1.06/3.68", "2.79/3.13", "1.000/1.120", "517.2/836.5"],
        ),
    ]:
        table_row(label, study, ["full", "zenfs_alloc"], expected)
    save("measured_runs", records)
    save("summary", summary)
    save("accounting", checks)
    save("pressure_summary", pressure)
    save("cache_comparison", cache)
    save("hint_precision", hints)
    save("paper_claims", claims)
    save("table", table)
    table_tex = [
        r"\begin{table}[t]",
        r"\centering",
        r"\caption{Placement quality and sensitivity.}",
        r"\label{tab:placement_controls}",
        r"\footnotesize",
        r"\setlength{\tabcolsep}{2.25pt}",
        r"\begin{tabular}{@{}lrrrr@{}}",
        r"\toprule",
        r"Policy or setting & Invalid & Tail & Host WA & $p_{99}$ \\",
        r" & (zones) & (zones) & & (ms) \\",
    ]
    for i, row in enumerate(table):
        if i in (0, 6, 10):
            heading = {
                0: "Placement and ablations, 84\\% load",
                6: "Span control: RetainZ / no span",
                10: "Scale/resources: RetainZ / ZenFS-Alloc",
            }[i]
            table_tex += [
                r"\midrule",
                r"\multicolumn{5}{@{}l}{\textit{" + heading + r"}} \\",
            ]
        label = row["label"].replace("%", r"\%")
        if i == 5:
            label = r"\textbf{RetainZ}"
        table_tex.append(" & ".join([label] + row["values"]) + r" \\")
    table_tex += [r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    (args.output / "placement_table.tex").write_text("\n".join(table_tex) + "\n")
    if args.check_paper:
        mismatches = [r for r in claims + table if not r["matches"]]
        assert not mismatches, json.dumps(mismatches, indent=2)
    print(
        json.dumps(
            dict(
                verified_runs=len(records),
                complete=sum(r["guest_success"] for r in records),
                capacity_stops=sum(not r["guest_success"] for r in records),
                claims=len(claims),
                table_rows=len(table),
                paper_values_match=all(r["matches"] for r in claims + table),
            )
        )
    )


if __name__ == "__main__":
    main()
