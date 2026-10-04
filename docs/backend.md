# Backend interface

The backend accepts a stream of checkpoint operations. It chooses placements
online, writes the data, and records commit/delete/relocation state in a durable
journal. The first three zones hold metadata; checkpoint data use the remaining
zones. Hints influence placement. Only a durable explicit deletion invalidates
a generation's extents.

## Trace format

Each line contains whitespace-separated fields:

| Operation | Fields | Meaning |
|---|---|---|
| `C` | `epoch key reclaim_hint lifetime logical_bytes [shard_bytes,...]` | Write all shards and commit one generation |
| `D` | `epoch key` | Durably delete a committed generation |
| `P` | `epoch key new_hint` | Update the retention hint of an existing generation |
| `R` | `epoch key` | Restore a committed generation and verify its bytes |
| `Q` | none | Finish the input and emit final counters |

`reclaim_hint` is a logical epoch or `inf` for retained anchors. Lifetime strings
(`short`, `medium`, `long`, `extreme`) supply the coarse hints used by the baseline.
The logical byte count equals the sum of the optional shard lengths. Each shard
is independently padded to 4 KiB for device I/O. Epochs share a workload timeline;
the runtime is responsible for mapping its local retention events onto it.

```text
C 1 generation1 4 short 8192 4096,4096
C 2 milestone inf extreme 4096
R 3 generation1
D 4 generation1
R 4 milestone
Q
```

## Local example

The file-backed mode implements zoned-device semantics for correctness tests:

```sh
make
mkdir -p results/example
printf 'C 1 c1 4 short 8192\nR 2 c1\nD 4 c1\nQ\n' > results/example/input.tsv
build/retainz results/example/device.img results/example/input.tsv retainz \
  1048576 results/example/run --file-zones 16 --meta-zones 3 \
  --max-open 4 --max-active 8 --adaptive --pressure-fallback --zenfs-resources
```

The Linux device path uses direct I/O and the kernel's zone-management ioctls.
`tools/run_femu.py` supplies it inside the disposable FEMU guest. A fresh run
expects an empty namespace; `--recover` reconstructs the existing checkpoint
state from the device journal.

## Placement controls

`--adaptive` enables retention-horizon spans and the tail weight. Small-generation
span adjustment is part of the full allocator. `--pressure-fallback` permits
compatible anchor/finite tails to be considered after normal selection finds no
candidate. `--borrow-reserve` enables the common transactional admission rule:
the reserve can be used only when the remaining padded checkpoint fits.
`--zenfs-resources` applies the shared resource reservations used in the paper.

The `--experiment` values are `full`, `exact_epoch`, `dbt_only`, `tail_only`,
`no_span`, and `no_density`. They operate within the same I/O, journal, GC, and
admission path. The `isolation` policy provides the separate-generation control;
`zenfs_alloc` uses the upstream-derived allocator. Experiment manifests select
the intended options, including the fixed-reserve and common-admission protocols.

## Persistence and outputs

A checkpoint becomes visible after its data and journal commit are durable.
An interrupted replacement does not make its predecessor unreadable. Recovery
replays committed changes and durable FINISH intent on the same namespace.
Copying GC records the relocated extents before reclaiming the old zone.

The output prefix produces per-operation counters (`.csv`), successful placement
and reclamation events (`.events.jsonl`), final zone states (`.zones.csv`), and
delete-to-reset observations (`.reclamation.csv`). These event records are the
measurement evidence used by the analyzer. `run.json` records the experiment
configuration and exit status; the launcher also saves the final summary.

The `--fault` options intentionally exit at selected persistence boundaries so
the tests can launch a new process and verify recovery. `--payload-image` and `--payload-index` select real captured
checkpoint bytes; `--restore-image` exports recovered bytes for the DCP verifier.
