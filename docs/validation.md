# Release validation

The public tree was built independently of the research workspace. The following
checks were executed using its source files and commands:

- Archived analysis validated 235 declared runs, including 16 capacity stops,
  and reproduced all 22 checked scalar values and all 14 rows of the paper table.
- Regenerating the 69 non-payload traces produced identical input bytes.
- The C++ backend passed 26 functional tests, the placement controls, 64
  scan/cached equivalence traces, and the upstream ZenFS allocator comparison.
- The pinned Megatron/TorchTitan callbacks passed the filesystem retention test.
- All nine experiment binaries and the backing-store checker built statically
  with GCC on the Linux reference host.
- The public FEMU launcher completed a new paired 84%-load comparison and six
  process-crash recovery cases. Independent event/byte accounting passed.
- A fresh eight-generation SmolLM2-135M capture was replayed through RetainZ with
  Megatron retention. The retained generations were compared byte for byte;
  model, AdamW, RNG, and the next CPU update matched the reference checkpoint.
- Both numerical figures were regenerated from the archived measurements.

The new payload capture used a different save directory. Its DCP metadata was
four bytes longer per generation; the tensor shard lengths and padded image
size were unchanged. The launcher records and uses the newly captured trace
lengths. Historical paper measurements remain associated with their original
inputs. The 430.2-KiB metadata comparison is described precisely in
[Paper claims](paper-claims.md#metadata-wording).

Reproduction commands and the distinction between recorded and freshly measured
timings are documented in [Reproduction](reproduction.md).
