#!/usr/bin/env python3
"""Apply the separately documented lossless capacity-experiment backend once."""

from pathlib import Path
import argparse

p = argparse.ArgumentParser()
p.add_argument("femu")
a = p.parse_args()
root = Path(a.femu) / "hw/femu/backend"
h = root / "dram.h"
c = root / "dram.c"
original = c.read_text()
assert "RZ_CAPACITY_BACKEND" not in original, "patch already applied"
h.write_text(
    h.read_text().replace(
        "void    *logical_space;", "void    *logical_space;\n    void    *rz_sparse;"
    )
)
helper = (Path(__file__).resolve().parents[1] / "femu/sparse_backend.inc").read_text()
text = original.replace(
    "/* Coperd: FEMU Memory Backend",
    "#define RZ_CAPACITY_BACKEND 1\n" + helper + "\n/* Coperd: FEMU Memory Backend",
)
text = text.replace(
    "b->size = nbytes;",
    """b->size = nbytes;
    if (getenv("RETAINZ_SPARSE_BACKEND")) {
        RzSparse *s = g_malloc0(sizeof(*s));
        s->count = (nbytes + RZ_CHUNK - 1) / RZ_CHUNK;
        s->blocks = g_new0(RzBlock, s->count);
        s->cached = UINT64_MAX;
        g_mutex_init(&s->lock);
        b->rz_sparse = s;
        femu_log("RetainZ lossless sparse capacity backend: %ld MiB\\n", (long)(nbytes >> 20));
        return 0;
    }""",
)
text = text.replace(
    "    if (b->logical_space) {\n        munlock",
    """    if (b->rz_sparse) {
        RzSparse *s = b->rz_sparse;
        femu_log("RetainZ capacity backend DMA write=%lu read=%lu compressed=%lu\\n",
                 s->dma_write, s->dma_read, s->stored);
        for (uint64_t i = 0; i < s->count; ++i) g_free(s->blocks[i].bytes);
        g_free(s->blocks); g_mutex_clear(&s->lock); g_free(s);
    }
    if (b->logical_space) {
        munlock""",
)
text = text.replace(
    "    void *mb = b->logical_space;",
    "    void *mb = b->logical_space;\n    RzSparse *rz = b->rz_sparse;\n    if (rz) g_mutex_lock(&rz->lock);",
)
text = text.replace(
    "            return -ERANGE;",
    "            if (rz) g_mutex_unlock(&rz->lock);\n            return -ERANGE;",
)
text = text.replace(
    """        if (dma_memory_rw(qsg->as, cur_addr, mb + mb_oft, cur_len, dir,
                          MEMTXATTRS_UNSPECIFIED)) {""",
    """        if (rz ? rz_dma(rz, qsg, cur_addr, mb_oft, cur_len, is_write) :
                  dma_memory_rw(qsg->as, cur_addr, mb + mb_oft, cur_len, dir,
                          MEMTXATTRS_UNSPECIFIED)) {""",
)
text = text.replace(
    "            return -EIO;",
    "            if (rz) g_mutex_unlock(&rz->lock);\n            return -EIO;",
)
text = text.replace(
    "    qemu_sglist_destroy(qsg);\n\n    return 0;",
    "    qemu_sglist_destroy(qsg);\n    if (rz) g_mutex_unlock(&rz->lock);\n\n    return 0;",
)
c.write_text(text)
