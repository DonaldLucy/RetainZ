#!/usr/bin/env python3
"""Run the declared FEMU experiments sequentially on Linux."""

import argparse
import fcntl
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def extract(console, out):
    """Keep machine measurements separate from transient VM diagnostics."""
    for name, body in re.findall(
        r"^RETAINZ_FILE_BEGIN=([^\r\n]+)\r?\n(.*?)^RETAINZ_FILE_END=\1\r?$",
        console,
        re.M | re.S,
    ):
        if Path(name).name != name:
            raise ValueError(f"Invalid guest output name: {name}")
        lines = body.splitlines()
        if name.endswith(".jsonl"):
            # A known clocksource notice can interleave with serial JSONL.
            notice = re.compile(
                r"\[\s*\d+\.\d+\] clocksource: Long readout interval, skipping watchdog check: cs_nsec: \d+ wd_nsec: \d+"
            )
            lines = [line for line in lines if not notice.fullmatch(line)]
            for line in lines:
                json.loads(line)
        (out / name).write_text("\n".join(lines) + "\n")
    for kind, filename in [
        ("RESULT", "result.summary.json"),
        ("METADATA_HEAP", "metadata.json"),
        ("FEMU_CODEC_RESULT", "codec.json"),
    ]:
        matches = re.findall(r"^" + kind + r" (\{.*\})\s*$", console, re.M)
        if matches:
            (out / filename).write_text(
                json.dumps(json.loads(matches[0]), indent=2) + "\n"
            )
    for stem, body in re.findall(
        r"^RETAINZ_RECOVERY_(\w+)_CSV_BEGIN\r?\n(.*?)^RETAINZ_RECOVERY_\1_CSV_END\r?$",
        console,
        re.M | re.S,
    ):
        (out / (stem + ".csv")).write_text(body.replace("\r", ""))


def run(case, args):
    out = args.output / case["name"]
    out.mkdir(parents=True, exist_ok=False)
    guest = ROOT / "build/guests" / case["name"]
    guest.mkdir(parents=True, exist_ok=True)
    for folder in ["bin", "dev", "proc", "sys", "tmp"]:
        (guest / folder).mkdir(exist_ok=True)
    shutil.copy(args.busybox, guest / "bin/busybox")
    if not (guest / "bin/sh").is_symlink():
        (guest / "bin/sh").symlink_to("busybox")
    shutil.copy(ROOT / case["bench"], guest / "online_bench")
    shutil.copy(ROOT / "build/femu_codec", guest / "femu_codec_test")
    shutil.copytree(
        ROOT / "tests/recovery_traces", guest / "recovery", dirs_exist_ok=True
    )
    shutil.copy(ROOT / case["trace"], guest / "trace.tsv")
    if case.get("payload_image"):
        # DCP stores its output path in .metadata. Use actual regenerated file
        # lengths while checking the intended retention event sequence.
        captured = args.payload_dir / (case["runtime"] + ".tsv")

        def operations(path):
            rows = [line.split() for line in path.read_text().splitlines()]
            return [f[:5] if f[0] == "C" else f for f in rows]

        if operations(captured) != operations(ROOT / case["trace"]):
            raise ValueError(
                "Regenerated retention events differ from the declared payload workload."
            )
        shutil.copy(captured, out / "input.tsv")
        shutil.copy(captured, guest / "trace.tsv")
        shutil.copy(args.payload_dir / "capture.json", out / "capture.json")
        shutil.copy(args.payload_dir / "payload.index", guest / "payload.index")
        shutil.copy(
            args.payload_dir / Path(case["restore_trace"]).name, guest / "restore.tsv"
        )
    shutil.copy(ROOT / case["init"], guest / "init")
    (guest / "init").chmod(0o755)
    paths = (
        b"\0".join(str(p.relative_to(guest)).encode() for p in sorted(guest.rglob("*")))
        + b"\0"
    )
    archive = subprocess.run(
        ["cpio", "--null", "-o", "--format=newc", "--quiet"],
        cwd=guest,
        input=paths,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout
    ram = guest.with_suffix(".gz")
    with ram.open("wb") as f:
        f.write(gzip.compress(archive, compresslevel=1, mtime=0))
    cap, zone = case["capacity_mib"], case["zone_bytes"]
    count = cap * 1048576 // zone
    groups = 1
    while count // groups > 255:
        groups *= 2
    assert count % groups == 0
    channels = max(2, groups)
    device = (
        f"femu,devsz_mb={cap},femu_mode=3,zns_num_ch={channels},zns_num_lun=4,"
        f"zns_num_plane=1,zns_num_blk={count // groups},zns_chnls_per_zone={channels // groups},"
        f'zns_flash_type=1,zns_max_open={case["open"]},zns_max_active={case["active"]},'
        "zns_pg_wr_lat=200000,zns_pg_rd_lat=40000,zns_blk_er_lat=2000000"
    )
    compressed = case.get("backing") == "compressed"
    qemu = args.qemu_capacity if compressed else args.qemu_dram
    append = (
        f'console=ttyS0 rdinit=/init panic=-1 rz_policy={case["policy"]} rz_zone={zone} '
        f'rz_open={case["open"]} rz_active={case["active"]} '
        f'rz_cadence={case.get("cadence_ms", 0)} rz_timeout={case["timeout"] - 30} '
        + case.get("kernel_extra", "")
    )
    command = [
        "/usr/bin/time",
        "-v",
        "nice",
        "-n",
        str(case.get("nice", 19)),
        "timeout",
        str(case["timeout"]),
        str(qemu),
        "-L",
        str(args.bios),
        "-machine",
        "q35,accel=tcg",
        "-cpu",
        "max",
        "-smp",
        "2",
        "-m",
        "512M",
        "-nographic",
        "-no-reboot",
        "-nodefaults",
        "-serial",
        "stdio",
        "-monitor",
        "none",
        "-kernel",
        str(args.kernel),
        "-initrd",
        str(ram),
        "-append",
        append,
        "-device",
        device,
    ]
    if case.get("payload_image"):
        payload = args.payload_dir / "payload.img"
        restored = out / "restored.img"
        with restored.open("wb") as f:
            f.truncate(payload.stat().st_size)
        command += [
            "-drive",
            f"file={payload},format=raw,if=none,id=payload,readonly=on,cache=none",
            "-device",
            "virtio-blk-pci,drive=payload",
            "-drive",
            f"file={restored},format=raw,if=none,id=restored,cache=none",
            "-device",
            "virtio-blk-pci,drive=restored",
        ]
    env = os.environ.copy()
    env.pop("RETAINZ_SPARSE_BACKEND", None)
    if compressed:
        env["RETAINZ_SPARSE_BACKEND"] = "1"
    start = time.monotonic()
    # All public runner instances share this lock, including capacity trials.
    with (ROOT / "build/femu-execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with (out / "console.log").open("w") as f:
            code = subprocess.run(
                command, stdout=f, stderr=subprocess.STDOUT, env=env
            ).returncode
    console = (out / "console.log").read_text(errors="replace")
    extract(console, out)
    markers = dict(re.findall(r"^(RETAINZ_[A-Z_]+)=(-?\d+)\s*$", console, re.M))
    success = (
        markers.get("RETAINZ_EXIT") == "0"
        or markers.get("RETAINZ_RECOVERY_EXIT") == "0"
    )
    status = dict(
        case=case,
        execution_workers=1,
        elapsed_seconds=time.monotonic() - start,
        process_exit=code,
        guest_success=success,
        recovery_after_capacity=markers.get("RETAINZ_FAILURE_RESTORE_EXIT") == "0",
        stop_reason=(
            None
            if success
            else "capacity" if "ERROR ENOSPC:" in console else "execution_error"
        ),
    )
    if case.get("payload_image"):
        path = out / "input.tsv"
        status["input_trace"] = (
            str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
        )
    (out / "run.json").write_text(json.dumps(status, indent=2) + "\n")
    if case.get("study") == "recovery":
        (out / "recovery.json").write_text(json.dumps(markers, indent=2) + "\n")
    print(
        json.dumps(
            dict(
                case=case["name"],
                complete=success,
                capacity_readback=status["recovery_after_capacity"],
                process_exit=code,
            )
        ),
        flush=True,
    )
    expected_capacity_stop = (
        status["stop_reason"] == "capacity" and status["recovery_after_capacity"]
    )
    if code != 0 or (not success and not expected_capacity_stop):
        raise RuntimeError(f'{case["name"]} failed; inspect {out / "console.log"}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    parser.add_argument(
        "--case", action="append", help="Exact case name; may be repeated."
    )
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--qemu-dram", type=Path, required=True)
    parser.add_argument("--qemu-capacity", type=Path)
    parser.add_argument("--bios", type=Path, required=True)
    parser.add_argument("--busybox", type=Path, default=Path("/usr/bin/busybox"))
    parser.add_argument("--output", type=Path, default=ROOT / "results/runs")
    parser.add_argument("--payload-dir", type=Path, default=ROOT / "build/checkpoints")
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text())
    if args.case:
        requested = set(args.case)
        cases = [c for c in cases if c["name"] in requested]
        if {c["name"] for c in cases} != requested:
            parser.error("Unknown case name.")
    for field in (
        "kernel",
        "qemu_dram",
        "qemu_capacity",
        "bios",
        "busybox",
        "output",
        "payload_dir",
    ):
        value = getattr(args, field)
        if value is not None:
            setattr(args, field, value.resolve())
    needed = [
        args.kernel,
        args.qemu_dram,
        args.bios,
        args.busybox,
        ROOT / "build/femu_codec",
    ]
    if any(c.get("backing") == "compressed" for c in cases):
        if args.qemu_capacity is None:
            parser.error("Capacity cases require --qemu-capacity.")
        needed.append(args.qemu_capacity)
    for case in cases:
        needed.extend(ROOT / case[key] for key in ("bench", "trace", "init"))
        if (args.output / case["name"]).exists():
            parser.error(
                f"Output already exists for {case['name']}; choose a new --output directory."
            )
    for path in needed:
        if not path.exists():
            parser.error(f"Missing input: {path}")
    for case in cases:
        run(case, args)


if __name__ == "__main__":
    main()
