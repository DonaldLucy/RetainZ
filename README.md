# RetainZ

**Reclaim-Time-Aware Placement for AI Checkpoints on Zoned SSDs**

RetainZ uses checkpoint retirement conditions supplied by the training runtime
to choose which generations share zones. Its allocator balances delayed
reclamation against unused zone capacity. The C++ backend executes writes,
copying garbage collection, journal commits, deletion, and recovery.

This repository contains the implementation, matched placement controls,
experiment inputs, and measurements behind the paper. Start with the commands
below; detailed protocols and claim-to-input mappings are in
[Reproduction](docs/reproduction.md) and [Paper claims](docs/paper-claims.md).

## Recompute the paper results

Python 3.12 is the reference interpreter. Numerical analysis uses only the
standard library and works without FEMU or model downloads.

```sh
python3 tools/analyze.py --check-paper
python3 tools/verify_recovery.py
python3 tools/rebuild_inputs.py
```

The first command validates all 235 recorded runs, including capacity stops,
then reconstructs the pressure results, mechanism controls, and the 14-row
placement table. It checks the rounded paper values against the measurements.
Outputs are written to `results/analysis/`; the input records remain in `data/`.

To regenerate the figures and a pasteable LaTeX table:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
make figures PYTHON=.venv/bin/python
```

Outputs:

- `results/figures/pressure.pdf`: fixed-reserve pressure sweep.
- `results/figures/mechanisms.pdf`: allocation cost, hint precision, and common admission.
- `results/analysis/placement_table.tex`: single-column placement and sensitivity table.
- `results/analysis/paper_claims.json`: computed values and their individual input runs.

The plots use portable DejaVu Serif fonts. Their data and dimensions match the
paper plots; font rendering can differ from the manuscript's typography.

## Build and test

A C++17 compiler and Python 3.12 are sufficient for the local correctness tests
on Linux or macOS.

```sh
make -j2 test
python3 tools/lifecycle_example.py
python3 tools/fetch_runtime_sources.py
python3 tests/runtime_retention.py
```

Tests cover committed and interrupted writes, payload readback, garbage
collection, FINISH recovery, allocation controls, cached-summary equivalence,
and comparison with the actual upstream ZenFS allocation functions.

`src/retainz.cpp` is the full backend. The source variants listed in
[Reproduction](docs/reproduction.md#source-variants) preserve the implementations
used for separate performance measurements. The [backend interface](docs/backend.md)
describes the trace format and options.

## Run new FEMU experiments

Use an x86-64 Linux host with a static BusyBox, a bootable Linux kernel, and the
pinned FEMU build described in [Reproduction](docs/reproduction.md#femu-environment).
The runner creates a disposable guest namespace and processes cases sequentially.

```sh
make -j2 benchmarks LDFLAGS=-static
python3 tools/run_femu.py experiments/pressure.json \
  --kernel /path/to/bzImage \
  --qemu-dram /path/to/qemu-system-x86_64 \
  --bios /path/to/FEMU/pc-bios
python3 tools/verify_measurements.py --input results/runs
```

Replace `experiments/pressure.json` with a study manifest to select another
experiment. `experiments/paper.json` declares the complete measured matrix.
Real-payload tests additionally require the capture step below; capacity tests
require `--qemu-capacity` pointing to the lossless-backing build.
The runner refuses to overwrite existing outputs; use `--output` for repeats.

## Capture and recover actual checkpoints

The real-payload path uses SmolLM2-135M, WikiText-2, AdamW, and PyTorch Distributed
Checkpoint. It executes pinned Megatron and TorchTitan retention callbacks,
writes the captured bytes through FEMU, restores model/optimizer/RNG state,
and compares one resumed CPU training update with the original checkpoint.

```sh
.venv/bin/pip install -r requirements-runtime.txt
.venv/bin/python tools/fetch_runtime_sources.py
.venv/bin/python tools/capture_payload.py
# Run experiments/payload.json with tools/run_femu.py and the same FEMU paths.
.venv/bin/python tools/verify_payload.py payload--payload_megatron_retainz_pressure
```

Run `verify_payload.py` for each of the four case names in
`experiments/payload.json`. Model and dataset revisions are pinned in the capture
tools. Downloads and generated tensor files stay in ignored cache/build paths.

## Measurement scope

The main comparison uses **ZenFS-Alloc**, a port of the upstream ZenFS data-file
allocator within the shared backend. The paper's model-derived capacity trials
use original Pythia-1B shard lengths with verifiable pattern bytes. The separate
135M recovery trials transfer actual checkpoint tensors. FEMU latency describes
the emulated storage experiment; the resumed-update check establishes recovery
correctness. See the reproduction document for aggregation, environment, and
measurement limits.

## License and authors

RetainZ is licensed under [Apache-2.0](LICENSE). Third-party code retains its
original licenses; see [THIRD_PARTY.md](THIRD_PARTY.md).

Minxing Chu — Harvard University and Brown University

Ruoxi Yang — Tufts University
