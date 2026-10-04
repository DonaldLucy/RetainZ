// Lossless FEMU backing-store validation. Run only on the empty disposable
// guest namespace, before the benchmark; this is not a performance test.
#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <linux/blkzoned.h>
#include <stdexcept>
#include <sys/ioctl.h>
#include <unistd.h>
#include <vector>
using U = uint64_t;
static constexpr U N = 2 * 1024 * 1024, B = 4096;
static void need(bool b, const char *s) {
  if (!b)
    throw std::runtime_error(s);
}
static void syscall_ok(bool b, const char *s) {
  if (!b)
    throw std::runtime_error(std::string(s) + ": " + strerror(errno));
}
static void io(int fd, void *p, U n, U off, bool write) {
  U done = 0;
  while (done < n) {
    ssize_t r = write ? pwrite(fd, (char *)p + done, n - done, off + done)
                      : pread(fd, (char *)p + done, n - done, off + done);
    if (r < 0 && errno == EINTR)
      continue;
    syscall_ok(r > 0, write ? "write" : "read");
    done += U(r);
  }
}
static U rng(U &v) {
  v ^= v << 13;
  v ^= v >> 7;
  v ^= v << 17;
  return v;
}
static blk_zone first_zone(int fd) {
  std::vector<unsigned char> b(sizeof(blk_zone_report) + sizeof(blk_zone));
  auto *r = (blk_zone_report *)b.data();
  r->nr_zones = 1;
  syscall_ok(ioctl(fd, BLKREPORTZONE, r) == 0, "report zone");
  need(r->nr_zones == 1, "no first zone");
  return r->zones[0];
}
int main(int argc, char **argv) {
  try {
    need(argc == 2, "usage: femu_codec_test /dev/nvme0n1");
    int fd = open(argv[1], O_RDWR | O_DIRECT);
    syscall_ok(fd >= 0, "open");
    auto zone = first_zone(fd);
    need(zone.type == BLK_ZONE_TYPE_SEQWRITE_REQ &&
             zone.cond == BLK_ZONE_COND_EMPTY,
         "requires empty disposable sequential zone");
    need(zone.capacity * 512 >= N,
         "first zone capacity must be at least 2 MiB");
    U base = zone.start * 512;
    blk_zone_range range{zone.start, zone.len};
    void *expected = nullptr, *actual = nullptr;
    syscall_ok(posix_memalign(&expected, B, N) == 0, "expected buffer");
    syscall_ok(posix_memalign(&actual, B, 128 * 1024) == 0, "read buffer");
    U *words = (U *)expected;
    U compared = 0, written = 0;
    for (unsigned round = 0; round < 2; round++) {
      U state = 0x9e3779b97f4a7c15ULL + round;
      for (U i = 0; i < N / 8; i++) {
        U off = i * 8, kind = off / (512 * 1024);
        if (kind == 0)
          words[i] = rng(state);
        else if (kind == 1)
          words[i] = round ? ~U(0) : 0;
        else if (kind == 2)
          words[i] = (U(round + 1) << 48) ^ i ^ 0xd1b54a32d192ed03ULL;
        else {
          U sub = (off / 4096) % 3;
          words[i] = sub == 0   ? rng(state)
                     : sub == 1 ? 0
                                : (i ^ 0x0123456789abcdefULL);
        }
      }
      syscall_ok(ioctl(fd, BLKOPENZONE, &range) == 0, "open zone");
      // 128 KiB writes starting 4 KiB into a compressed chunk exercise writes
      // spanning both 64 KiB cache boundaries and the raw/encoded transitions.
      const U sizes[] = {4096, 131072, 57344, 8192, 65536, 122880};
      U p = 0, k = 0;
      while (p < N) {
        U n = std::min<U>(sizes[k++ % 6], N - p);
        io(fd, (char *)expected + p, n, base + p, true);
        p += n;
        written += n;
      }
      syscall_ok(fsync(fd) == 0, "flush data");
      // A permutation touches every 4 KiB block exactly once while forcing
      // repeated dirty-cache eviction, decompression and raw-fallback reload.
      for (U i = 0; i < N / B; i++) {
        U block = (i * 251) % (N / B), off = block * B;
        io(fd, actual, B, base + off, false);
        need(memcmp(actual, (char *)expected + off, B) == 0,
             "permuted read payload mismatch");
        compared += B;
      }
      for (U off : {U(61440), U(520192), U(1044480), U(1568768)}) {
        io(fd, actual, 128 * 1024, base + off, false);
        need(memcmp(actual, (char *)expected + off, 128 * 1024) == 0,
             "cross-chunk read payload mismatch");
        compared += 128 * 1024;
      }
      syscall_ok(ioctl(fd, BLKRESETZONE, &range) == 0, "reset zone");
      auto after = first_zone(fd);
      need(after.cond == BLK_ZONE_COND_EMPTY && after.wp == after.start,
           "reset did not return empty zone");
    }
    std::cout << "FEMU_CODEC_RESULT "
                 "{\"verified\":true,\"rounds\":2,\"written_bytes\":"
              << written << ",\"compared_bytes\":" << compared
              << ",\"patterns\":[\"pseudorandom\",\"zero\",\"ones\","
                 "\"counter\",\"mixed\"],\"zone_empty_on_exit\":true}\n";
    free(expected);
    free(actual);
    close(fd);
    return 0;
  } catch (const std::exception &e) {
    std::cerr << "FEMU_CODEC_ERROR " << e.what() << "\n";
    return 1;
  }
}
