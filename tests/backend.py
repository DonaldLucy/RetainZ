#!/usr/bin/env python3
"""Real backend functional tests using its explicitly labelled file device.
Checks semantic retention, copying-GC preservation, and process recovery.
FEMU performance is tested separately by the experiment launcher.
"""

import csv
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(os.environ.get("RETAINZ_ONLINE_BIN", str(ROOT / "build/retainz")))


class OnlineBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="retainz-online-")
        self.d = Path(self.tmp.name)
        self.dev = self.d / "device"
        self.calls = 0

    def tearDown(self):
        self.tmp.cleanup()

    def run_trace(self, trace, policy="retainz", zones=16, extra=(), expect=0):
        self.calls += 1
        stem = self.d / f"run{self.calls}"
        tr = stem.with_suffix(".tsv")
        tr.write_text(trace)
        r = subprocess.run(
            [
                str(BIN),
                str(self.dev),
                str(tr),
                policy,
                "65536",
                str(stem),
                "--file-zones",
                str(zones),
                "--meta-zones",
                "3",
                "--max-open",
                "4",
                "--max-active",
                "8",
                *extra,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, expect, r.stdout + r.stderr)
        rows = (
            list(csv.DictReader(stem.with_suffix(".csv").read_text().splitlines()))
            if stem.with_suffix(".csv").exists()
            else []
        )
        return r, rows

    def test_shards_delete_promotion_restore(self):
        _, rows = self.run_trace(
            "C 1 a 4 short 98317 32769,65548\nC 2 b 5 short 120000\nD 2 a\nP 2 b inf\nR 2 b\nQ\n"
        )
        self.assertEqual(rows[-1]["shards_written"], "3")
        self.assertEqual(rows[-2]["verified"], "1")
        self.assertEqual(int(rows[-2]["restore_bytes"]), 120000)
        self.assertEqual(int(rows[-1]["logical_bytes"]), 218317)
        self.assertEqual(int(rows[-1]["live_bytes"]), 122880)

    def test_each_baseline(self):
        for policy in ("fifo", "isolation", "coarse", "retainz", "zenfs_alloc"):
            with self.subTest(policy=policy):
                self.dev = self.d / f"device-{policy}"
                _, rows = self.run_trace(
                    "C 1 a 4 short 100000\nC 2 b inf extreme 100000\nD 2 a\nR 2 b\nQ\n",
                    policy,
                )
                self.assertEqual(rows[-2]["verified"], "1")
                self.assertGreater(int(rows[-1]["resets"]), 0)

    def write_zones(self):
        logs = [
            json.loads(x)
            for x in (self.d / f"run{self.calls}.events.jsonl").read_text().splitlines()
        ]
        return {r["key"]: r["zone"] for r in logs if r["event"] == "write"}

    def test_zenfs_prefers_longer_lifetime_and_last_equal_candidate(self):
        self.run_trace(
            "C 1 a 30 long 8192\nC 2 b 31 long 8192\nC 3 c 6 short 8192\nR 3 c\nQ\n",
            "zenfs_alloc",
            extra=("--zenfs-resources",),
        )
        zones = self.write_zones()
        self.assertNotEqual(zones["a"], zones["b"])
        self.assertEqual(zones["b"], zones["c"])

    def test_zenfs_active_exhaustion_reuses_equal_lifetime(self):
        trace = (
            "".join(f"C {i} c{i} {i+3} short 4096\n" for i in range(1, 8))
            + "R 7 c7\nQ\n"
        )
        _, rows = self.run_trace(trace, "zenfs_alloc", extra=("--zenfs-resources",))
        zones = self.write_zones()
        self.assertEqual(len(set(zones.values())), 6)
        self.assertEqual(zones["c6"], zones["c7"])
        self.assertLessEqual(int(rows[-1]["peak_active"]), 7)  # 6 data + metadata

    def test_zenfs_tag_survives_delete_promotion_and_restart(self):
        self.run_trace(
            "C 1 a 30 long 32768\nC 2 b 5 short 8192\nD 3 a\nP 3 b inf\nQ\n",
            "zenfs_alloc",
            extra=("--zenfs-resources",),
        )
        zone = self.write_zones()["a"]

        def tag():
            data = list(
                csv.DictReader(
                    (self.d / f"run{self.calls}.zones.csv").read_text().splitlines()
                )
            )
            return int(next(r for r in data if int(r["zone"]) == zone)["lifetime_hint"])

        self.assertEqual(tag(), 4)
        self.run_trace(
            "R 4 b\nC 4 c 7 short 8192\nR 4 c\nQ\n",
            "zenfs_alloc",
            extra=("--recover", "--zenfs-resources"),
        )
        self.assertEqual(self.write_zones()["c"], zone)
        self.assertEqual(tag(), 4)

    def test_zenfs_gc_and_relocation_crash_preserve_payload(self):
        for point in ("aftercopy", "afterrelocate"):
            self.dev = self.d / f"zenfs-{point}"
            lines = [f"C {i} c{i} {i+30} long 32768" for i in range(1, 7)]
            lines += [f"C {i} c{i} {i+3} short 32768" for i in range(7, 13)]
            lines += [f"D 13 c{i}" for i in range(1, 7)]
            lines += ["C 14 newest 44 long 65536", "Q"]
            self.run_trace(
                "\n".join(lines) + "\n",
                "zenfs_alloc",
                zones=10,
                extra=("--zenfs-resources", "--fault", point, "--fault-key", "newest"),
                expect=86,
            )
            _, rows = self.run_trace(
                "R 15 c7\nR 15 c12\nC 16 next 46 long 65536\nR 16 next\nQ\n",
                "zenfs_alloc",
                zones=10,
                extra=("--recover", "--zenfs-resources"),
            )
            self.assertTrue(
                all(x["verified"] == "1" for x in rows if x["event"] == "R")
            )

    def test_no_reserve_control_exposes_all_empty_candidates(self):
        for policy in ("zenfs_alloc", "retainz"):
            for unreserved in (False, True):
                self.dev = self.d / f"reserve-control-{policy}-{unreserved}"
                extra = ("--zenfs-resources",) + (
                    ("--no-empty-reserve",) if unreserved else ()
                )
                self.run_trace(
                    "C 1 a 20 long 65536\nC 2 b 20 long 65536\nC 3 c 20 long 65536\nC 4 d 20 long 65536\nR 4 d\nQ\n",
                    policy,
                    zones=7,
                    extra=extra,
                    expect=0 if unreserved else 1,
                )
                if unreserved:
                    self.run_trace(
                        "R 5 d\nQ\n", policy, zones=7, extra=extra + ("--recover",)
                    )

    def test_gc_preserves_payload(self):
        # Fill 6 data zones with 12 half-zone checkpoints, delete alternate
        # generations, then insert larger generations while keeping survivors.
        lines = []
        for i in range(1, 13):
            lines.append(f"C {i} c{i} {i+30} short 32768")
        for i in range(1, 13, 2):
            lines.append(f"D 13 c{i}")
        for i in range(14, 17):
            lines.append(f"C {i} c{i} {i+30} short 65536")
        for i in (2, 4, 6, 8, 10, 12, 14, 15, 16):
            lines.append(f"R 17 c{i}")
        lines.append("Q")
        _, rows = self.run_trace("\n".join(lines) + "\n", "fifo", zones=10)
        self.assertGreater(int(rows[-1]["gc_write_bytes"]), 0)
        self.assertTrue(all(x["verified"] == "1" for x in rows if x["event"] == "R"))

    def actual_payload(self, keys):
        image = self.d / "payload.img"
        index = self.d / "payload.index"
        output = self.d / "restored.img"
        blocks = {key: os.urandom(65536) for key in keys}
        image.write_bytes(b"".join(blocks.values()))
        output.write_bytes(bytes(len(blocks) * 65536))
        index.write_text("".join(f"{key} {i*65536}\n" for i, key in enumerate(keys)))
        return (
            blocks,
            output,
            (
                "--payload-image",
                str(image),
                "--payload-index",
                str(index),
                "--restore-image",
                str(output),
            ),
        )

    def test_actual_payload_gc_restart_and_export(self):
        keys = [f"c{i}" for i in range(1, 17)]
        blocks, output, extra = self.actual_payload(keys)
        lines = [f"C {i} c{i} {i+30} short 32768" for i in range(1, 13)]
        lines += [f"D 13 c{i}" for i in range(1, 13, 2)]
        lines += [f"C {i} c{i} {i+30} short 65536" for i in range(14, 17)]
        _, rows = self.run_trace(
            "\n".join(lines + ["Q"]) + "\n", "fifo", zones=10, extra=extra
        )
        self.assertGreater(int(rows[-1]["gc_write_bytes"]), 0)
        survivors = [2, 4, 6, 8, 10, 12, 14, 15, 16]
        _, rows = self.run_trace(
            "".join(f"R 17 c{i}\n" for i in survivors) + "Q\n",
            "fifo",
            zones=10,
            extra=extra + ("--recover",),
        )
        restored = output.read_bytes()
        for i in survivors:
            n = 32768 if i < 14 else 65536
            self.assertEqual(
                restored[(i - 1) * 65536 : (i - 1) * 65536 + n], blocks[f"c{i}"][:n]
            )
        self.assertTrue(all(x["verified"] == "1" for x in rows if x["event"] == "R"))

    def test_actual_payload_uncommitted_data_keeps_predecessor(self):
        blocks, output, extra = self.actual_payload(["stable", "pending", "next"])
        self.run_trace(
            "C 1 stable 5 short 50001 20001,30000\nC 2 pending 6 short 65536\nQ\n",
            extra=extra + ("--fault", "beforecommit", "--fault-key", "pending"),
            expect=86,
        )
        self.run_trace(
            "R 3 stable\nC 4 next 8 short 65536\nR 4 next\nQ\n",
            extra=extra + ("--recover",),
        )
        restored = output.read_bytes()
        self.assertEqual(restored[:53248], blocks["stable"][:53248])
        self.assertEqual(restored[131072:196608], blocks["next"])
        self.run_trace("R 5 pending\nQ\n", extra=extra + ("--recover",), expect=1)

    def test_crash_boundaries(self):
        for point in ("afterwrite", "beforecommit", "aftercommit"):
            with self.subTest(point=point):
                self.dev = self.d / f"crash-{point}"
                self.run_trace(
                    "C 1 stable 5 short 98304\nC 2 pending 6 short 98304\nQ\n",
                    extra=("--fault", point, "--fault-key", "pending"),
                    expect=86,
                )
                recover = "R 3 stable\n"
                if point == "aftercommit":
                    recover += "R 3 pending\n"
                recover += "C 4 next 8 short 100000\nR 4 next\nQ\n"
                _, rows = self.run_trace(recover, extra=("--recover",))
                self.assertTrue(
                    all(x["verified"] == "1" for x in rows if x["event"] == "R")
                )
                if point != "aftercommit":
                    self.run_trace("R 5 pending\nQ\n", extra=("--recover",), expect=1)

    def test_recover_after_copy_gc(self):
        lines = [f"C {i} c{i} {i+30} short 32768" for i in range(1, 13)]
        lines += [f"D 13 c{i}" for i in range(1, 13, 2)]
        lines += ["C 14 newest 44 short 65536", "Q"]
        self.run_trace("\n".join(lines) + "\n", "fifo", zones=10)
        _, rows = self.run_trace(
            "R 15 c2\nR 15 c12\nR 15 newest\nQ\n",
            "fifo",
            zones=10,
            extra=("--recover",),
        )
        self.assertTrue(all(x["verified"] == "1" for x in rows if x["event"] == "R"))

    def test_borrow_reserve_completes_atomic_replacement(self):
        for policy in ("coarse", "retainz", "zenfs_alloc"):
            with self.subTest(policy=policy):
                self.dev = self.d / f"borrow-{policy}"
                seed = "C 0 resident inf extreme 196608\nC 1 old 3 short 65536\n"
                trace = (
                    seed + "C 2 next 5 short 40001 20001,20000\nD 2 old\nR 2 next\nQ\n"
                )
                options = ("--adaptive", "--borrow-reserve", "--zenfs-resources")
                if policy != "zenfs_alloc":
                    options += ("--pressure-fallback",)
                _, rows = self.run_trace(trace, policy, zones=8, extra=options)
                self.assertEqual(rows[-1]["reserve_borrows"], "1")
                self.assertEqual(rows[-1]["reserve_borrow_remaining_bytes"], "40960")
                self.assertTrue(
                    all(r["verified"] == "1" for r in rows if r["event"] == "R")
                )
                self.run_trace("R 3 next\nQ\n", policy, zones=8, extra=("--recover",))

    def test_borrow_reserve_requires_entire_padded_remainder(self):
        seed = "C 0 resident inf extreme 196608\nC 1 old 3 short 65536\n"
        _, rows = self.run_trace(
            seed + "C 2 too_large 5 short 80000 40000,40000\nQ\n",
            zones=8,
            extra=("--borrow-reserve",),
            expect=1,
        )
        self.assertEqual(rows[-1]["reserve_borrows"], "0")
        self.run_trace("R 3 old\nQ\n", zones=8, extra=("--recover",))

    def test_admission_drains_tails_with_spare_active_token(self):
        # The new generation is larger than one zone, but existing compatible
        # tails plus the reserve can hold it. ZenFS normally requests a fresh
        # zone when an active token is available; the reserve masks that zone.
        for policy in ("zenfs_alloc", "retainz"):
            for fault in ("", "afterwrite", "aftercommit"):
                self.dev = self.d / f"admission-{policy}-{fault}"
                trace = "C 0 resident inf extreme 196608\nC 1 old 3 short 98304\nC 2 next 5 short 80000 40000,40000\nQ\n"
                extra = ("--adaptive", "--zenfs-resources", "--borrow-reserve")
                if policy == "retainz":
                    extra += ("--pressure-fallback",)
                if fault:
                    extra += ("--fault", fault, "--fault-key", "next")
                self.run_trace(
                    trace, policy, zones=9, extra=extra, expect=86 if fault else 0
                )
                self.run_trace(
                    "R 3 old\nQ\n",
                    policy,
                    zones=9,
                    extra=("--recover", "--zenfs-resources"),
                )
                self.run_trace(
                    "R 3 next\nQ\n",
                    policy,
                    zones=9,
                    extra=("--recover", "--zenfs-resources"),
                    expect=1 if fault == "afterwrite" else 0,
                )

    def test_borrowed_uncommitted_data_does_not_replace_old(self):
        trace = "C 0 resident inf extreme 196608\nC 1 old 3 short 65536\nC 2 next 5 short 65536\nQ\n"
        self.run_trace(
            trace,
            zones=8,
            extra=("--borrow-reserve", "--fault", "afterwrite", "--fault-key", "next"),
            expect=86,
        )
        self.run_trace("R 3 old\nQ\n", zones=8, extra=("--recover",))
        self.run_trace("R 3 next\nQ\n", zones=8, extra=("--recover",), expect=1)

    def test_nearest_ablation_changes_expiry_weighting(self):
        targets = []
        for flag in ((), ("--nearest",)):
            self.dev = self.d / f"nearest-{len(targets)}"
            self.run_trace(
                "C 1 a 5 short 57344\nC 2 b 7 medium 40960\nQ\n", extra=("--adaptive",)
            )
            self.run_trace(
                "C 3 c 6 short 8192\nR 3 c\nQ\n",
                extra=("--recover", "--adaptive") + flag,
            )
            logs = [
                json.loads(x)
                for x in (self.d / f"run{self.calls}.events.jsonl")
                .read_text()
                .splitlines()
            ]
            targets.append(
                next(
                    r["zone"] for r in logs if r["event"] == "write" and r["key"] == "c"
                )
            )
        self.assertNotEqual(*targets)

    def test_anchor_mixing_ablation_preserves_live_anchor(self):
        trace = "C 0 resident inf extreme 196608\nC 1 finite 4 short 32768\nC 2 anchor inf extreme 32768\nD 4 finite\nR 4 anchor\nQ\n"
        _, rows = self.run_trace(
            trace, zones=8, extra=("--adaptive", "--pressure-fallback", "--mix-anchors")
        )
        self.assertEqual(rows[-1]["invalid_bytes"], "32768")
        self.run_trace("R 5 anchor\nQ\n", zones=8, extra=("--recover",))

    def test_reclamation_delay_starts_at_actual_delete(self):
        self.run_trace(
            "C 1 a 4 short 32768\nC 2 b 5 short 32768\nD 4 a\nD 5 b\nQ\n",
            extra=("--adaptive",),
        )
        data = list(
            csv.DictReader(
                (self.d / f"run{self.calls}.reclamation.csv").read_text().splitlines()
            )
        )
        self.assertEqual(sorted(int(r["delay_epochs"]) for r in data), [0, 1])
        self.assertTrue(all(r["delete_epoch_stamped"] == "1" for r in data))

    def test_gc_crash_boundaries(self):
        for point in ("aftercopy", "afterrelocate"):
            with self.subTest(point=point):
                self.dev = self.d / f"gc-crash-{point}"
                lines = [f"C {i} c{i} {i+30} short 32768" for i in range(1, 13)]
                lines += [f"D 13 c{i}" for i in range(1, 13, 2)]
                lines += ["C 14 interrupted 44 short 65536", "Q"]
                self.run_trace(
                    "\n".join(lines) + "\n",
                    "fifo",
                    zones=10,
                    extra=("--fault", point, "--fault-key", "interrupted"),
                    expect=86,
                )
                restore = "\n".join(f"R 15 c{i}" for i in (2, 4, 6, 8, 10, 12))
                _, rows = self.run_trace(
                    restore + "\nC 16 next 46 short 65536\nR 16 next\nQ\n",
                    "fifo",
                    zones=10,
                    extra=("--recover",),
                )
                self.assertTrue(
                    all(x["verified"] == "1" for x in rows if x["event"] == "R")
                )

    def test_recovery_replays_durable_finish_intent(self):
        trace = (
            "\n".join(f"C {i} c{i} {i*10} short 32768" for i in range(1, 5)) + "\nQ\n"
        )
        opts = ("--max-open", "2", "--max-active", "4", "--window", "1")
        self.run_trace(
            trace, zones=10, extra=opts + ("--fault", "afterfinishjournal"), expect=86
        )
        _, rows = self.run_trace(
            "R 5 c1\nR 5 c2\nR 5 c3\nQ\n", zones=10, extra=opts + ("--recover",)
        )
        self.assertEqual(rows[-1]["stranded_tail_bytes"], "32768")
        self.assertTrue(all(r["verified"] == "1" for r in rows if r["event"] == "R"))
        self.run_trace(
            "C 6 next 10 short 32768\nQ\n",
            zones=10,
            extra=opts + ("--recover", "--fault", "afterwrite", "--fault-key", "next"),
            expect=86,
        )
        self.run_trace(
            "R 7 c1\nC 7 later 10 short 32768\nR 7 later\nQ\n",
            zones=10,
            extra=opts + ("--recover",),
        )

    def test_multpage_journal_transaction(self):
        # More than 31 extent records exercises transaction pages, which real
        # DCP manifests with many shards can produce even for modest sizes.
        shards = ",".join(["4097"] * 40)
        self.run_trace(f"C 1 sharded 8 short {4097*40} {shards}\nQ\n", zones=24)
        _, rows = self.run_trace("R 2 sharded\nQ\n", zones=24, extra=("--recover",))
        self.assertEqual(rows[0]["verified"], "1")
        self.assertEqual(rows[0]["restore_bytes"], str(4097 * 40))

    def test_enospc_keeps_partial_counters_and_committed_data(self):
        r, rows = self.run_trace(
            "C 1 safe 5 short 65536\nC 2 too_big 6 short 262144\nQ\n", zones=8, expect=1
        )
        self.assertEqual(rows[-1]["status"], "failed")
        self.assertEqual(rows[-1]["logical_bytes"], "65536")
        self.assertGreater(int(rows[-1]["payload_bytes"]), 65536)
        self.assertIn('"committed_checkpoints":1', r.stdout)
        self.assertIn('"status":"failed"', r.stdout)
        self.run_trace("R 3 safe\nQ\n", zones=8, extra=("--recover",))

    def test_pressure_fallback_uses_finite_tails(self):
        # Four distinct finite windows each leave a half-zone tail. The fifth
        # checkpoint has no compatible window, but enough writable finite tail.
        lines = [f"C {i} c{i} {i*10} short 32768" for i in range(1, 6)]
        lines += [f"R 6 c{i}" for i in range(1, 6)] + ["Q"]
        trace = "\n".join(lines) + "\n"
        self.run_trace(trace, zones=8, expect=1)
        self.dev = self.d / "fallback"
        _, rows = self.run_trace(trace, zones=8, extra=("--pressure-fallback",))
        self.assertEqual(rows[-1]["pressure_fallback_allocations"], "1")
        self.assertEqual(rows[-1]["pressure_fallback_bytes"], "32768")
        self.assertTrue(all(r["verified"] == "1" for r in rows if r["event"] == "R"))
        self.run_trace(
            "R 7 c5\nQ\n", zones=8, extra=("--recover", "--pressure-fallback")
        )

    def test_coarse_pressure_fallback_has_same_admission_option(self):
        lines = [
            f"C {i} c{i} {10+i} {cl} 49152"
            for i, cl in enumerate(("short", "medium", "long", "extreme"), 1)
        ]
        lines += (
            ["C 5 next 15 short 32768"]
            + [f"R 6 c{i}" for i in range(1, 5)]
            + ["R 6 next", "Q"]
        )
        trace = "\n".join(lines) + "\n"
        self.run_trace(trace, "coarse", zones=8, expect=1)
        self.dev = self.d / "coarse-fallback"
        _, rows = self.run_trace(
            trace, "coarse", zones=8, extra=("--pressure-fallback",)
        )
        self.assertEqual(rows[-1]["pressure_fallback_allocations"], "1")
        self.assertEqual(rows[-1]["pressure_fallback_bytes"], "16384")
        self.assertTrue(all(r["verified"] == "1" for r in rows if r["event"] == "R"))

    def test_bad_opcode_rejected(self):
        self.run_trace("C 1 safe 4 short 65536\nRESET 2 safe\nQ\n", expect=1)
        self.run_trace("R 3 safe\nQ\n", extra=("--recover",))


if __name__ == "__main__":
    unittest.main(verbosity=2)
