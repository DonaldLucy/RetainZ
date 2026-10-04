# Third-party components

RetainZ-authored code is Apache-2.0. This grant does not replace the licenses of
third-party components or downloaded models and datasets.

| Component | Pinned revision | License / distribution |
|---|---|---|
| [ZenFS](https://github.com/westerndigitalcorporation/zenfs) | `919c2ebbcdc170525a9abffb8b61a3795b1e6ae5` | Upstream Apache-2.0 or GPL-2.0 options; this artifact uses the Apache option. Unmodified allocation source and both upstream license files are in `third_party/zenfs`. |
| [Megatron-LM](https://github.com/NVIDIA/Megatron-LM) | `d113016bb2850c4b7d804f24ba72c055d7d4f861` | Upstream BSD-style license, downloaded together with the required source file by `tools/fetch_runtime_sources.py`. |
| [TorchTitan](https://github.com/pytorch/torchtitan) | `35ceeb5901e6ffe6a0a3ecd4c4e6e9e80cbc853e` | BSD-3-Clause; the fetch tool retrieves source and license together. |
| [FEMU](https://github.com/MoatLab/FEMU) / QEMU | `5e1d9a88bc9dbfd582af27bf29c81c7774e3543d` | Built externally; retain upstream QEMU/FEMU licensing when distributing a combined build. |

The ZenFS adapter in `src/zenfs_allocator.hpp` preserves attribution. Its
independent equivalence test extracts unmodified upstream function bodies.
The retention driver dynamically extracts function bodies from the downloaded
Megatron/TorchTitan source. Those dependencies are optional and excluded from
the RetainZ source distribution.

Model weights, tokenizers, and WikiText-2 data are downloaded from their upstream
Hugging Face repositories at revisions recorded in the capture scripts. They
are not redistributed here. Generated checkpoint tensors and FEMU binaries are
also excluded. Archived workload lengths and measurement records are included
as RetainZ research data.
