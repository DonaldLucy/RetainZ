# Paper claims and measurement inputs

Run `python3 tools/analyze.py --check-paper` to rebuild the numerical evidence.
`results/analysis/paper_claims.json` lists every checked scalar with its precise
value, rounded paper value, and constituent case names. `table.json` lists the
14 table rows, including paired controls. Every output is derived from the
CSV/JSON records in `data/measurements` and the declared inputs in `experiments`.

| Paper result | Experiment family | Reconstructed output |
|---|---|---|
| Figure 1: one occupied zone versus two for the surviving checkpoints | `tools/lifecycle_example.py` | `results/lifecycle/verified.json` |
| 84% load: 6.53 → 3.80 reserved zones, 1.165 → 1.003 host WA, 85.3 → 44.8 ms p99 | `pressure` | `pressure_summary.json`, `paper_claims.json` |
| 41.7% capacity-overhead and 47.5% p99 reductions | Same paired 84% inputs | `paper_claims.json` |
| 90% load: 250 saves versus 40–49 before capacity exhaustion | `pressure` | `pressure_summary.json` and the recorded failure readbacks |
| Common admission: host WA 1.514 → 1.214 | `replacement_pressure` | `paper_claims.json` |
| Allocation cost 5.996 → 0.270 ms, including maintenance | `allocation_scan`, `allocation_cached`, matching `pressure` | `cache_comparison.json` |
| Hint precision: 3.80, 4.53, 4.99 zones | `pressure`, `hint_precision` | `hint_precision.json` |
| Same hints: 34.1% less overhead than equal-epoch best-fit | `placement` | `paper_claims.json`, `table.json` |
| Small-generation adjustment: 11.5% less overhead | `small_checkpoints` | `paper_claims.json` |
| Span control at 90%: 4.9% lower WA | `span_pressure` | `paper_claims.json`, `table.json` |
| 64-MiB zones: 43.4% lower overhead and 38.2% lower p99 | `zone_capacity_64m` | `paper_claims.json`, `table.json` |
| 430.2 KiB allocator metadata | `metadata` | `paper_claims.json`, original `metadata.json` |
| 64-GiB capacity completion with retained-byte readback | `replacement_capacity` | `measured_runs.json`, original restore rows |
| Model/AdamW/RNG restore and matching next update | `payload` | original `tensor_verification.json`, checked by the analyzer |
| Recovery across commit, relocation, and FINISH | `recovery` | `data/recovery`, `tools/verify_recovery.py` |

## Metadata wording

The two heap measurements compare **RetainZ and ZenFS-Alloc**, both using the
instrumented cached backend on the same 70%-load input. Each reports 440,480
usable bytes, or 430.2 KiB after rounding. They do not compare the history-scan
implementation with the cached implementation. The corresponding manuscript
sentence should read:

```tex
RetainZ and ZenFS-Alloc have the same measured allocator
metadata footprint of 430.2~KiB.
```

The probe includes checkpoint records/keys, per-generation extent indices,
extent storage, zone state, and device geometry. It excludes stack, I/O buffers,
measurement containers, allocator headers, and transient allocation peaks.
Process RSS and allocator metadata are distinct quantities. The current paper's
metadata figure should not be used to infer the memory cost of caching summaries.
