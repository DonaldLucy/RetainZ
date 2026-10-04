/* Lossless, sparse FEMU backing for capacity experiments under a 10-GiB RAM
 * budget. This is host emulation machinery, not SSD compression. Every DMA
 * byte is transferred and preserved. Never use its timings as DRAM-backend
 * timings. The normal FEMU path is unchanged when the opt-in is absent.
 */
#include <zlib.h>
#define RZ_CHUNK 65536
typedef struct RzBlock { unsigned char *bytes; uint32_t len; } RzBlock;
typedef struct RzSparse {
    RzBlock *blocks;
    uint64_t count, cached, stored, dma_read, dma_write;
    bool dirty;
    unsigned char cache[RZ_CHUNK], encoded[RZ_CHUNK], packed[RZ_CHUNK + 1024];
    GMutex lock;
} RzSparse;

static void rz_switch(RzSparse *s, uint64_t page)
{
    if (s->cached == page) return;
    if (s->dirty) {
        RzBlock *p = &s->blocks[s->cached];
        uLongf n = sizeof(s->packed);
        /* A reversible word-index XOR improves compressibility of counter
         * payloads while preserving arbitrary data, including random bytes. */
        for (uint64_t i = 0; i < RZ_CHUNK / 8; ++i) {
            uint64_t v; memcpy(&v, s->cache + 8*i, 8); v ^= i;
            memcpy(s->encoded + 8*i, &v, 8);
        }
        int rc = compress2(s->packed, &n, s->encoded, RZ_CHUNK, 1);
        assert(rc == Z_OK);
        s->stored -= p->len;
        g_free(p->bytes);
        p->len = MIN(n, RZ_CHUNK);
        p->bytes = g_memdup2(n < RZ_CHUNK ? s->packed : s->cache, p->len);
        s->stored += p->len;
    }
    RzBlock *p = &s->blocks[page];
    if (!p->bytes) memset(s->cache, 0, RZ_CHUNK);
    else if (p->len == RZ_CHUNK) memcpy(s->cache, p->bytes, RZ_CHUNK);
    else {
        uLongf n = RZ_CHUNK;
        int rc = uncompress(s->cache, &n, p->bytes, p->len);
        assert(rc == Z_OK && n == RZ_CHUNK);
        for (uint64_t i = 0; i < RZ_CHUNK / 8; ++i) {
            uint64_t v; memcpy(&v, s->cache + 8*i, 8); v ^= i;
            memcpy(s->cache + 8*i, &v, 8);
        }
    }
    s->cached = page;
    s->dirty = false;
}

static int rz_dma(RzSparse *s, QEMUSGList *qsg, dma_addr_t address,
                  uint64_t offset, uint64_t length, bool writing)
{
    while (length) {
        uint64_t page = offset / RZ_CHUNK, within = offset % RZ_CHUNK;
        uint64_t n = MIN(length, RZ_CHUNK - within);
        assert(page < s->count);
        rz_switch(s, page);
        if (dma_memory_rw(qsg->as, address, s->cache + within, n,
                writing ? DMA_DIRECTION_TO_DEVICE : DMA_DIRECTION_FROM_DEVICE,
                MEMTXATTRS_UNSPECIFIED)) return -EIO;
        if (writing) { s->dirty = true; s->dma_write += n; }
        else s->dma_read += n;
        address += n; offset += n; length -= n;
    }
    return 0;
}
