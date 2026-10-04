# Fixed allocation-policy source

Repository: https://github.com/westerndigitalcorporation/zenfs

Revision: `919c2ebbcdc170525a9abffb8b61a3795b1e6ae5`.

These files were exported with `git archive` from the previously built pinned
checkout. They are unmodified upstream files, without RetainZ's native-ZenFS
measurement patch. The allocation adapter uses the Apache-2.0 licensing option;
both upstream license files are retained.

`tests/zenfs_allocator.py` extracts six complete function bodies
directly from `fs/zbd_zenfs.cc` into an independent reference harness. The
production adapter in `src/zenfs_allocator.hpp` does not supply that oracle's
decisions. Concurrent waiting, device errors, native metadata and native GC are
outside this serial policy equivalence check.
