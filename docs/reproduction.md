# Reproduction

## Recorded measurements and new runs

`data/measurements` contains 235 declared runs with input configurations,
checkpoint measurements, zone states, and event records. Sixteen runs stop at
capacity exhaustion and retain both their partial counters and subsequent
readback evidence. `tools/analyze.py --check-paper` verifies input/event agreement,
byte counts, zone capacities, resource bounds, and restore records before
aggregating anything. The paper-value comparison is a check against the archived
observations, including their rounded presentation.

A new run executes the same input through the backend and FEMU. Wall-clock
latency and allocation time depend on the host and scheduler. Exact equality
to archived timing is therefore not an acceptance criterion for a fresh run.
Use `tools/verify_measurements.py --input results/runs` for a subset. After the
complete matrix and four tensor-verification steps have finished, use:

```sh
python3 tools/analyze.py --measurements results/runs --output results/new-analysis
```

Archive-derived figures use `results/analysis`. For newly measured figures, use:

```sh
python3 tools/plot_pressure.py --input results/new-analysis --output results/new-figures/pressure
python3 tools/plot_mechanisms.py --input results/new-analysis --output results/new-figures/mechanisms
```

## Experiment matrix

All paths in manifests are relative to the repository. `paper.json` is the
complete matrix; the other manifests are subsets, not additional runs.

| Manifest/study | Purpose |
|---|---|
| `pressure` | 70–90% target peak live load, fixed GC reserve, three inputs per load and policy |
| `allocation_scan`, `allocation_cached`, `metadata` | Selection/maintenance timing and final allocator heap |
| `hint_precision` | Same actual events with reclaim hints rounded to 4 or 16 epochs |
| `replacement_pressure` | 90% load with shared transactional admission |
| `placement` | Identical hints, cost-term ablations, span control, and generation isolation; five inputs |
| `small_checkpoints` | Size-adjusted spans and packing controls; five inputs |
| `pressure_controls`, `delayed_deletion`, `extended_retention` | High pressure and disagreements between placement hints and actual retirement |
| `span_*` | Paired span controls on three additional inputs |
| `zone_reference`, `active_budget_*`, `zone_capacity_64m` | One-factor zone-size and active-budget controls |
| `capacity`, `replacement_capacity` | Unscaled Pythia-1B shard lengths on 64–256 GiB namespaces |
| `payload` | Actual 135M checkpoint bytes, two retention callbacks and two policies |
| `pressure_repeats`, `placement_repeats` | Repeated execution of an unchanged input |

`experiments/recovery.json` separately exercises process crashes at data write,
pre-commit, post-commit, copy, relocation, and FINISH-journal boundaries. Its
records are in `data/recovery`; `tools/verify_recovery.py` checks the fresh-process
readbacks and the visibility of interrupted generations.

## Source variants

Each manifest names its compiled source through `bench`. These files are kept
separate so that instrumentation and placement refinements do not silently change
the program associated with a recorded timing result.

| Source | Role |
|---|---|
| `retainz.cpp` | Full allocator and explicit placement ablations used by the main control/sensitivity matrix |
| `placement_reference.cpp` | Full-rule reference without the ablation switch, used for equivalence tests |
| `fixed_reserve.cpp` | Pressure sweep, hint precision, and model-derived capacity measurements |
| `history_scan.cpp` | Full-history selection control |
| `summary_accounting.cpp` | Cached selection plus separately timed summary maintenance |
| `metadata.cpp` | Final allocator-owned usable-heap measurement on Linux |
| `payload.cpp` | Actual checkpoint-byte round trips |
| `replacement_capacity.cpp` | Common admission for the 64-GiB capacity comparison |
| `replacement_pressure.cpp` | Common admission for the 90% pressure comparison |

The small-generation span adjustment is inactive in the main pressure inputs,
whose generations are at least half a zone. Resource reservations, admission
mode, and experiment controls are explicit in each case's `kernel_extra`.

## FEMU environment

The reference host exposes 26 logical CPUs (Intel Xeon Platinum 8481C).
Reference execution used Ubuntu 24.04.3, GCC 13.3.0, static C++17 binaries
(`-O2 -static`), BusyBox 1.36.1, and a Linux 7.0.0-1011-gcp guest kernel.
FEMU revision is `5e1d9a88bc9dbfd582af27bf29c81c7774e3543d` from
[MoatLab/FEMU](https://github.com/MoatLab/FEMU). Its QEMU reports version 10.1.0.
The guest uses TCG, two virtual CPUs, and 512 MiB of RAM. The main namespace is
256 MiB with 4-MiB zones. The device's open/active limits are 4/8; shared
reservations leave one open and six active slots for checkpoint data.
Larger active-budget controls leave 14 or 30 data slots. The 64-MiB-zone control
scales the namespace and checkpoint bytes by 16 while preserving zone counts.

The launcher records the requested scheduling priority in every case; archived
cases used nested low-priority launches. Run new timing trials with an otherwise
idle host and no other FEMU experiments. The public launcher serializes its runs
using a shared file lock. Large capacity measurements may have been run together
in the archived experiment; those records are excluded from latency comparisons.

The namespace is emulated entirely inside QEMU. `/dev/nvme0n1` in the guest is the
FEMU device. The runner has no host block-device argument.

Install normal FEMU build dependencies on Ubuntu:

```sh
sudo apt-get install build-essential git ninja-build python3-venv pkg-config \
  libglib2.0-dev libpixman-1-dev libslirp-dev zlib1g-dev busybox-static cpio
git clone https://github.com/MoatLab/FEMU.git third_party/FEMU
git -C third_party/FEMU checkout 5e1d9a88bc9dbfd582af27bf29c81c7774e3543d
mkdir -p third_party/FEMU/build-dram
cd third_party/FEMU/build-dram
../configure --target-list=x86_64-softmmu --disable-werror --disable-docs \
  --disable-gtk --disable-sdl --enable-slirp
make -j2
cd ../../..
make -j2 benchmarks LDFLAGS=-static
```

Supply an x86-64 `bzImage` with built-in NVMe, ZNS, devtmpfs, initramfs, and
virtio block support. The reference kernel is named above; a kernel requiring
external NVMe modules needs those modules in the initramfs. This artifact's
minimal guest assumes built-in drivers.

The runner specifies flash write/read/erase delays of 200,000/40,000/2,000,000 ns.
The complete geometry is computed from the manifest and passed to FEMU. It runs
the backend inside a fresh guest, then extracts CSV/JSON outputs. Transient boot
diagnostics stay in the ignored result directory. A paired smoke experiment is:

```sh
python3 tools/run_femu.py experiments/pressure.json \
  --case pressure--tenants_r3p2_p84_s0_zenfs_alloc \
  --case pressure--tenants_r3p2_p84_s0_retainz_pressure \
  --kernel /path/to/bzImage \
  --qemu-dram third_party/FEMU/build-dram/qemu-system-x86_64 \
  --bios third_party/FEMU/pc-bios --output results/smoke
python3 tools/verify_measurements.py --input results/smoke
```

### Large-capacity backing

Build the original DRAM executable first, then apply the included backing-store
change and build into a different directory. Preserve the original executable:

```sh
mkdir -p build
cp third_party/FEMU/build-dram/qemu-system-x86_64 build/qemu-dram
python3 tools/prepare_capacity_backend.py third_party/FEMU
mkdir -p third_party/FEMU/build-capacity
cd third_party/FEMU/build-capacity
../configure --target-list=x86_64-softmmu --disable-werror --disable-docs \
  --disable-gtk --disable-sdl --enable-slirp
make -j2
cd ../../..
```

Pass the second executable as `--qemu-capacity`. Cases tagged `compressed` enable
lossless host-side backing, with raw fallback for incompressible data. This
reduces host memory requirements for large logical namespaces. It preserves
transferred byte values; it is not an SSD compression proposal. Large-capacity
throughput/latency is not pooled with the DRAM-backed pressure results.
`build/femu_codec` checks random, constant, counter, and mixed bytes across
compressed-block boundaries when a case requests `rz_codec=1`.

## Workload and metric definitions

Trace files declare creates, deletes, promotions, and reads; they contain no
precomputed zone choices. The four-stream synthetic family requests 250 saves
over 120 epochs, with a 100-ms minimum start-to-start interval. Resident anchors
set target peak live occupancy after scheduled deletions. This target is not
the instantaneous device occupancy during a replacement write.

For each run, synthetic statistics exclude the resident prefill and epochs
through 20. Reserved overhead is the epoch-weighted mean of invalid bytes plus
unused tails, divided by zone capacity. Both writable and FINISH-stranded tails
are counted, with stranded tails also recorded separately. Host WA includes
payload, copying, and journal writes per logical checkpoint byte; it excludes
the prefill/warmup in the synthetic plots. Checkpoint latency covers the measured
backend operation. Compute p99 within each run by linear interpolation, then
average run p99s. Error bars span the smallest and largest per-input result.
Failed prefixes retain their counters but do not enter full-trace timing means.

The cache control includes selection and summary-maintenance time. Its event
sequences are compared with the scan implementation after removing measured
timestamps. Heap probes measure the final usable heap attributable to allocator
state; their timings are excluded from the cost comparison.

## Actual checkpoint data

`requirements-runtime.txt` records the Python package versions used by the CPU
capture and restore path, which was executed with Python 3.10.21. Use a separate
Python 3.10 environment when matching that serialization environment exactly. Model and WikiText-2 revisions are pinned in the tools.
SmolLM2-135M capture uses BF16, AdamW, two CPU threads, eight saves, 32-token
sequences, and four DCP writer threads. Pythia-1B capture uses the recorded
optimizer-in-backward procedure and eight DCP writer threads. Its measured shard
lengths are in `data/pythia/manifests.jsonl`; regenerate with:

```sh
.venv/bin/python tools/capture_pythia.py --output build/pythia --epochs 24
```

Megatron's pinned callback retains the latest save plus periodic exemptions and
deletes after save completion. TorchTitan's callback keeps recent saves with
exemptions and purges before saving. The harness executes these actual function
bodies, supplying local rank/filesystem plumbing and joining deletion workers.
It preserves their different event ordering. `data/payload/*_events.json`
contains the observed events; synthetic latest-K workloads have their own
explicit commit-before-delete ordering.

Fresh payload capture goes to `build/checkpoints`, leaving the archived manifest
intact. DCP embeds the save path in its `.metadata` file, so changing directories
can slightly change the metadata length. The runner uses the freshly observed
shard lengths, checks retention-event agreement, and stores that actual trace
alongside the run. `run.json` identifies it through `input_trace`; the analyzer
uses this field when present. Plan for at least 60 GiB of disk space when retaining all four FEMU exports
and reconstructed DCP folders. Generated tensors and downloaded model weights
are not part of the Git repository. The checker compares every retained file byte
for byte, loads the latest recovered model/AdamW/RNG state, and compares the next
CPU update's loss, parameters, and optimizer state.

## Measurement limits

The timing environment is CPU-emulated FEMU, not a physical SSD or a GPU training
cluster. ZenFS-Alloc isolates the upstream data-file allocation policy in a
shared I/O, journal, and GC path; it does not represent the full native ZenFS
filesystem. The runtime integration executes retention callbacks with a local
trainer. Payload recovery establishes byte/state/update equivalence, not
distributed-training throughput. The 1B capacity workload preserves real shard
sizes using pattern bytes; actual-tensor round trips use the 135M model.
