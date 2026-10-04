// RetainZ online checkpoint backend. Linux raw-ZNS path; --file-zones is a
// deterministic functional device used only by tests, never as FEMU evidence.
#include "zenfs_allocator.hpp"
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <thread>
#include <unistd.h>
#include <vector>
#ifdef __linux__
#include <linux/blkzoned.h>
#include <sys/ioctl.h>
#include <sys/resource.h>
#endif
using U = uint64_t;
static constexpr U INF = (U(1) << 60), BS = 4096, BUFSZ = 131072;
static U now_ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}
static void need(bool b, const std::string &s) {
  if (!b)
    throw std::runtime_error(s);
}
static void sysneed(bool b, const std::string &s) {
  if (!b)
    throw std::runtime_error(s + ": " + strerror(errno));
}
static U align_up(U n) {
  need(n <= std::numeric_limits<U>::max() - BS, "size overflow");
  return (n + BS - 1) / BS * BS;
}
static void all_io(int fd, void *buf, size_t n, U off, bool write) {
  size_t p = 0;
  while (p < n) {
    ssize_t r = write ? pwrite(fd, (char *)buf + p, n - p, off + p)
                      : pread(fd, (char *)buf + p, n - p, off + p);
    if (r < 0 && errno == EINTR)
      continue;
    sysneed(r > 0, write ? "pwrite" : "pread");
    p += size_t(r);
  }
}
static void fill_pattern(void *buf, size_t n, U id, U logical) {
  need(logical % 8 == 0 && n % 8 == 0, "pattern alignment");
  auto *p = (U *)buf;
  for (size_t i = 0; i < n / 8; i++)
    p[i] = (id << 48) ^ (logical / 8 + i) ^ 0xd1b54a32d192ed03ULL;
}
static bool verify_pattern(void *buf, size_t n, U id, U logical) {
  auto *p = (U *)buf;
  for (size_t i = 0; i < n / 8; i++)
    if (p[i] != ((id << 48) ^ (logical / 8 + i) ^ 0xd1b54a32d192ed03ULL))
      return false;
  return true;
}
struct Geo {
  U start = 0, len = 0, cap = 0, wp = 0;
  int state = 0;
}; // 0 empty, 1 open, 2 closed, 3 full
struct Device {
  int fd = -1, sfd = -1;
  bool mock = false;
  std::string path;
  std::vector<Geo> g;
  Device(std::string p, U C, int filezones)
      : mock(filezones > 0), path(std::move(p)) {
    int flags = O_RDWR;
    if (mock)
      flags |= O_CREAT;
#ifdef __linux__
    if (!mock)
      flags |= O_DIRECT;
#endif
    fd = open(path.c_str(), flags, 0600);
    sysneed(fd >= 0, "open device");
    if (mock) {
      struct stat st{};
      sysneed(fstat(fd, &st) == 0, "stat device");
      bool fresh = st.st_size == 0;
      sysneed(ftruncate(fd, U(filezones) * C) == 0, "size mock device");
      sfd = open((path + ".zones").c_str(), O_CREAT | O_RDWR, 0600);
      sysneed(sfd >= 0, "open mock geometry");
      for (int i = 0; i < filezones; i++) {
        Geo z;
        z.start = U(i) * C;
        z.cap = z.len = C;
        z.wp = z.start;
        if (!fresh) {
          std::array<U, 2> d{};
          all_io(sfd, d.data(), sizeof(d), i * sizeof(d), false);
          z.wp = d[0];
          z.state = int(d[1]);
        }
        g.push_back(z);
        if (fresh)
          save(i);
      }
      return;
    }
#ifdef __linux__
    U sector = 0;
    for (;;) {
      std::vector<unsigned char> b(sizeof(blk_zone_report) +
                                   256 * sizeof(blk_zone));
      auto *r = (blk_zone_report *)b.data();
      r->sector = sector;
      r->nr_zones = 256;
      sysneed(ioctl(fd, BLKREPORTZONE, r) == 0, "BLKREPORTZONE");
      if (!r->nr_zones)
        break;
      for (unsigned i = 0; i < r->nr_zones; i++) {
        auto &z = r->zones[i];
        need(z.type == BLK_ZONE_TYPE_SEQWRITE_REQ,
             "requires sequential-write-required zones");
        Geo q;
        q.start = z.start * 512;
        q.len = z.len * 512;
        q.cap = z.capacity * 512;
        need(q.len == C && q.cap > 0 && q.cap <= q.len, "unexpected geometry");
        q.wp = z.wp * 512;
        switch (z.cond) {
        case BLK_ZONE_COND_EMPTY:
          q.state = 0;
          q.wp = q.start;
          break;
        case BLK_ZONE_COND_IMP_OPEN:
        case BLK_ZONE_COND_EXP_OPEN:
          q.state = 1;
          break;
        case BLK_ZONE_COND_CLOSED:
          q.state = 2;
          break;
        case BLK_ZONE_COND_FULL:
          q.state = 3;
          q.wp = q.start + q.cap;
          break;
        default:
          throw std::runtime_error("unwritable zone state");
        }
        g.push_back(q);
      }
      sector = r->zones[r->nr_zones - 1].start + r->zones[r->nr_zones - 1].len;
    }
    need(!g.empty(), "no zones");
#else
    throw std::runtime_error("real zoned devices require Linux");
#endif
  }
  ~Device() {
    if (fd >= 0)
      close(fd);
    if (sfd >= 0)
      close(sfd);
  }
  void save(int z) {
    if (mock) {
      std::array<U, 2> d{g[z].wp, U(g[z].state)};
      all_io(sfd, d.data(), sizeof(d), z * sizeof(d), true);
    }
  }
  void command(int z, char op) {
    need(z >= 0 && size_t(z) < g.size(), "zone index");
    auto &q = g[z];
#ifdef __linux__
    if (!mock) {
      blk_zone_range r{q.start / 512, q.len / 512};
      unsigned long cmd = op == 'o'   ? BLKOPENZONE
                          : op == 'c' ? BLKCLOSEZONE
                          : op == 'f' ? BLKFINISHZONE
                                      : BLKRESETZONE;
      sysneed(ioctl(fd, cmd, &r) == 0, std::string("zone ") + op);
    }
#endif
    if (op == 'o')
      q.state = 1;
    else if (op == 'c')
      q.state = 2;
    else if (op == 'f') {
      q.state = 3;
      q.wp = q.start + q.cap;
    } else if (op == 'r') {
      q.state = 0;
      q.wp = q.start;
    } else
      throw std::runtime_error("unknown device command");
    save(z);
  }
  void write(int z, U off, void *buf, U n) {
    auto &q = g.at(z);
    need(off == q.wp - q.start && n % BS == 0 && off + n <= q.cap,
         "sequential write or capacity violation");
    need(q.state == 1, "write requires open zone");
    all_io(fd, buf, n, q.start + off, true);
    q.wp += n;
    if (q.wp - q.start == q.cap)
      q.state = 3;
    save(z);
  }
  void read(int z, U off, void *buf, U n) {
    need(off + n <= g.at(z).cap, "read range");
    all_io(fd, buf, n, g[z].start + off, false);
  }
  void sync() {
    sysneed(fsync(fd) == 0, "device fsync");
    if (mock)
      sysneed(fsync(sfd) == 0, "mock geometry fsync");
  }
};
// Fixed records are packed into 4 KiB pages. A transaction takes effect only
// when its final page is present. This targets process crash/restart, not torn
// sector or arbitrary device power-loss guarantees.
struct Rec {
  U type = 0;
  char key[56]{};
  U a = 0, b = 0, c = 0, d = 0, e = 0, f = 0, g = 0, h = 0;
};
static_assert(sizeof(Rec) == 128, "journal record size");
struct Page {
  U magic = 0, seq = 0, txn = 0, count = 0, final = 0;
  Rec r[31]{};
  char pad[88]{};
};
static_assert(sizeof(Page) == BS, "journal page size");
static constexpr U MAGIC = 0x325a4e4941544552ULL;
static Rec rec(char type, const std::string &key = "") {
  need(key.size() < 56, "checkpoint key must be at most 55 bytes");
  Rec r;
  r.type = type;
  memcpy(r.key, key.data(), key.size());
  return r;
}
struct Ext {
  std::string key;
  int z = 0;
  U off = 0, n = 0, logical = 0;
  bool live = true;
  U deleted_ns = 0, deleted_epoch = 0;
};
struct Checkpoint {
  U id = 0, epoch = 0, death = 0, size = 0;
  int cl = 0;
  bool live = true, committed = true;
  U stored = 0, nshards = 1, payload_start = 0;
  std::vector<size_t> ext;
};
struct Zone {
  U written = 0, erases = 0, last = 0;
  bool finished = false;
  int lifetime = 0;
  std::array<U, 5> hints{INF, 0, INF, INF, 0};
};
struct Options {
  int meta = 3, maxopen = 4, maxactive = 8, filezones = 0;
  bool no_empty_reserve = false, zenfs_resources = false, recover = false,
       adaptive = false, resource_adaptive = false, pressure_fallback = false,
       borrow_reserve = false, nearest = false, mix_anchors = false;
  U window = 2, cadence_ms = 0;
  std::string fault, faultkey, payload_image, payload_index, restore_image;
};
struct Counters {
  U logical = 0, payload = 0, gcwrite = 0, meta = 0, read = 0, alloc = 0,
    hint_update = 0, shards = 0, commits = 0, fallbacks = 0, fallback_bytes = 0,
    reserve_borrows = 0, reserve_borrow_bytes = 0, write_ns = 0, gc_ns = 0,
    sync_ns = 0, resets = 0, closes = 0, finishes = 0, peakopen = 0,
    peakactive = 0, peaklive = 0, peakreserved = 0;
};
struct State {
  U live = 0, invalid = 0, tail = 0, stranded = 0, free = 0, empty = 0,
    open = 0, active = 0, reserved = 0;
};
struct Backend {
  Device dev;
  Options o;
  std::string policy;
  std::vector<Zone> z;
  std::map<std::string, Checkpoint> cp;
  std::vector<Ext> ex;
  Counters c;
  void *buf = nullptr;
  void *pagebuf = nullptr;
  U seq = 0, txn = 0, nextid = 1, tick = 0, epoch = 0;
  int mz = 0;
  U moff = 0;
  std::ofstream csv, events;
  U last_sample = 0, last_invalid = 0, last_checkpoint_start = 0;
  std::string outprefix;
  long double dbt = 0;
  std::vector<std::array<U, 3>> delays;
  double recovery_s = 0;
  bool recovering = false;
  int payload_fd = -1, restore_fd = -1;
  void *verifybuf = nullptr;
  std::map<std::string, U> payload_offsets;
  Backend(std::string path, U C, std::string p, std::string prefix,
          Options opts)
      : dev(path, C, opts.filezones), o(opts), policy(p), z(dev.g.size()) {
    need(o.meta >= 1 && dev.g.size() >= size_t(o.meta + 3),
         "requires metadata, GC reserve, and at least 2 data zones");
    need(o.maxopen >= 2 && o.maxactive >= o.maxopen,
         "need >=2 open and active>=open");
    need(policy == "fifo" || policy == "isolation" || policy == "coarse" ||
             policy == "retainz" || policy == "zenfs_alloc",
         "unknown policy");
    need(o.window >= 1, "window must be positive");
    need(!o.zenfs_resources || o.maxopen >= 4,
         "ZenFS resource accounting needs at least 4 open zones");
    need(!o.pressure_fallback || policy == "retainz" || policy == "coarse",
         "pressure fallback requires RetainZ or Coarse");
    sysneed(posix_memalign(&buf, BS, BUFSZ) == 0, "I/O buffer allocation");
    sysneed(posix_memalign(&pagebuf, BS, BS) == 0, "journal buffer allocation");
    if (!o.payload_image.empty()) {
      payload_fd = open(o.payload_image.c_str(), O_RDONLY);
      sysneed(payload_fd >= 0, "open payload image");
      sysneed(posix_memalign(&verifybuf, BS, BUFSZ) == 0,
              "payload verification buffer");
      if (!o.payload_index.empty()) {
        std::ifstream index(o.payload_index);
        need(index.good(), "open payload index");
        std::string key;
        U off;
        while (index >> key) {
          need(bool(index >> off) && off % BS == 0, "invalid payload index");
          need(payload_offsets.emplace(key, off).second,
               "duplicate payload key");
        }
        need(index.eof(), "read payload index");
      }
      if (!o.restore_image.empty()) {
        restore_fd = open(o.restore_image.c_str(), O_RDWR);
        sysneed(restore_fd >= 0, "open restore output image");
      }
    } else
      need(o.payload_index.empty() && o.restore_image.empty(),
           "payload index/output require payload image");
    outprefix = prefix;
    csv.open(prefix + ".csv");
    events.open(prefix + ".events.jsonl");
    need(csv.good() && events.good(), "open outputs");
    csv << "event,epoch,key,status,checkpoint_seconds,allocator_ns,write_io_ns,"
           "gc_ns,sync_ns,logical_bytes,payload_bytes,gc_write_bytes,metadata_"
           "write_bytes,read_bytes,invalid_bytes,tail_bytes,stranded_tail_"
           "bytes,live_bytes,free_bytes,empty_zones,open,active,peak_open,peak_"
           "active,peak_live_bytes,peak_reserved_bytes,dbt_byte_seconds,resets,"
           "erase_max,erase_cv,restore_io_seconds,verify_seconds,restore_bytes,"
           "verified,recovery_seconds,allocated_payload_estimate_bytes,shards_"
           "written,process_peak_rss_bytes,pressure_fallback_allocations,"
           "pressure_fallback_bytes,reserve_borrows,reserve_borrow_remaining_"
           "bytes,hint_update_ns\n";
    csv << std::setprecision(12);
    if (o.recover) {
      U t = now_ns();
      recover();
      recovery_s = (now_ns() - t) / 1e9;
    } else
      for (auto &q : dev.g)
        need(q.state == 0, "fresh run requires every device zone empty");
    last_sample = now_ns();
    sample();
  }
  ~Backend() {
    free(buf);
    free(pagebuf);
    free(verifybuf);
    if (payload_fd >= 0)
      close(payload_fd);
    if (restore_fd >= 0)
      close(restore_fd);
  }
  U capacity(int i) const { return dev.g[i].cap; }
  State state() const {
    State s;
    std::vector<U> live(z.size());
    for (auto &e : ex)
      if (e.live)
        live[e.z] += e.n;
    for (size_t i = 0; i < z.size(); i++) {
      auto &g = dev.g[i];
      s.open += g.state == 1;
      s.active += g.state == 1 || g.state == 2;
      if (i < size_t(o.meta))
        continue;
      s.live += live[i];
      s.invalid += z[i].written - live[i];
      if (g.state == 0) {
        s.empty++;
        s.free += g.cap;
      } else {
        s.reserved += g.cap;
        U tail = g.cap - z[i].written;
        s.tail += tail;
        if (g.state == 3)
          s.stranded += tail;
        else
          s.free += tail;
      }
    }
    return s;
  }
  void sample() {
    U t = now_ns();
    auto s = state();
    if (last_sample)
      dbt += (long double)last_invalid * (t - last_sample) / 1e9;
    last_sample = t;
    last_invalid = s.invalid;
    c.peakopen = std::max(c.peakopen, s.open);
    c.peakactive = std::max(c.peakactive, s.active);
    c.peaklive = std::max(c.peaklive, s.live);
    c.peakreserved = std::max(c.peakreserved, s.reserved);
    need(s.open <= U(o.maxopen) && s.active <= U(o.maxactive),
         "zone resource limit exceeded");
  }
  void sync() {
    U t = now_ns();
    dev.sync();
    c.sync_ns += now_ns() - t;
  }
  void log(const std::string &op, const std::string &key, int zid, U n) {
    events << "{\"event\":\"" << op << "\",\"epoch\":" << epoch << ",\"key\":\""
           << key << "\",\"zone\":" << zid << ",\"bytes\":" << n
           << ",\"time_ns\":" << now_ns() << "}\n";
  }
  void journal(const std::vector<Rec> &r, bool final = true) {
    need(!r.empty(), "empty transaction");
    U tx = ++txn;
    size_t p = 0;
    while (p < r.size()) {
      while (mz < o.meta && moff + BS > capacity(mz)) {
        mz++;
        moff = 0;
      }
      need(mz < o.meta, "journal exhausted; increase --meta-zones for this "
                        "bounded experiment");
      if (dev.g[mz].state != 1)
        dev.command(mz, 'o');
      auto *pg = (Page *)pagebuf;
      memset(pg, 0, BS);
      pg->magic = MAGIC;
      pg->seq = ++seq;
      pg->txn = tx;
      pg->count = std::min<size_t>(31, r.size() - p);
      pg->final = final && (p + pg->count == r.size());
      for (U i = 0; i < pg->count; i++)
        pg->r[i] = r[p + i];
      dev.write(mz, moff, pg, BS);
      moff += BS;
      c.meta += BS;
      p += pg->count;
      sample();
    }
    sync();
  }
  void apply(const std::vector<Rec> &rs) {
    std::vector<bool> dirty(z.size());
    for (auto &r : rs) {
      std::string key(r.key, strnlen(r.key, 56));
      switch (r.type) {
      case 'B': {
        Checkpoint q;
        q.id = r.a;
        q.epoch = r.b;
        q.death = r.c;
        q.size = r.d;
        q.cl = int(r.e);
        q.stored = r.f;
        q.nshards = r.g;
        q.payload_start = r.h;
        cp[key] = q;
        nextid = std::max(nextid, q.id + 1);
        break;
      }
      case 'E': {
        need(cp.count(key), "journal extent without checkpoint");
        Ext e;
        e.key = key;
        e.z = int(r.a);
        e.off = r.b;
        e.n = r.c;
        e.logical = r.d;
        cp[key].ext.push_back(ex.size());
        ex.push_back(e);
        z[e.z].written = std::max(z[e.z].written, e.off + e.n);
        if (r.e)
          z[e.z].lifetime = int(r.e);
        add_hint(e.z, cp.at(key));
        break;
      }
      case 'D': {
        need(cp.count(key) && cp[key].live, "journal delete target");
        cp[key].live = false;
        for (auto i : cp[key].ext) {
          ex[i].live = false;
          ex[i].deleted_ns = r.a;
          ex[i].deleted_epoch = r.b;
        }
        break;
      }
      case 'P':
        need(cp.count(key) && cp[key].live, "journal promotion target");
        cp[key].death = r.a;
        if (r.a >= INF)
          cp[key].cl = 3;
        for (auto i : cp[key].ext)
          if (ex[i].n)
            dirty[ex[i].z] = true;
        break;
      case 'M': {
        bool found = false;
        for (auto i : cp.at(key).ext) {
          auto &e = ex[i];
          if (e.live && e.z == int(r.a) && e.off == r.b && e.logical == r.f) {
            dirty[e.z] = true;
            dirty[int(r.c)] = true;
            e.z = int(r.c);
            e.off = r.d;
            z[e.z].written = std::max(z[e.z].written, e.off + e.n);
            if (r.g)
              z[e.z].lifetime = int(r.g);
            found = true;
            break;
          }
        }
        need(found, "journal relocation target");
        break;
      }
      case 'F':
        z.at(r.a).written = r.b;
        z[r.a].finished = true;
        break;
      case 'Z': {
        int zi = int(r.a);
        z.at(zi).written = 0;
        z[zi].finished = false;
        z[zi].lifetime = 0;
        z[zi].hints = {INF, 0, INF, INF, 0};
        z[zi].erases++;
        for (auto &e : ex)
          if (e.z == zi && !e.live) {
            e.n = 0;
          }
        break;
      }
      default:
        throw std::runtime_error("unknown journal record");
      }
    }
    for (size_t i = o.meta; i < z.size(); i++)
      if (dirty[i])
        rebuild_hint(int(i));
  }
  void recover() {
    recovering = true;
    std::vector<Rec> pending;
    U ptx = 0;
    bool ended = false;
    for (int j = 0; j < o.meta && !ended; j++) {
      U end = dev.g[j].wp - dev.g[j].start;
      for (U off = 0; off + BS <= end; off += BS) {
        dev.read(j, off, pagebuf, BS);
        auto *pg = (Page *)pagebuf;
        need(pg->magic == MAGIC && pg->count <= 31 && pg->seq == seq + 1,
             "invalid journal page");
        seq = pg->seq;
        txn = std::max(txn, pg->txn);
        if (pg->txn != ptx) {
          pending.clear();
          ptx = pg->txn;
        }
        for (U k = 0; k < pg->count; k++)
          pending.push_back(pg->r[k]);
        if (pg->final) {
          apply(pending);
          pending.clear();
        }
        moff = off + BS;
        mz = j;
      }
      if (end < capacity(j)) {
        moff = end;
        mz = j;
        ended = true;
      }
    }
    // A crash may leave data without a committed mapping. Preserve actual write
    // pointers; reclaim only zones with no references in committed metadata.
    for (size_t i = o.meta; i < z.size(); i++) {
      U wp = dev.g[i].wp - dev.g[i].start;
      if (!z[i].finished)
        z[i].written = std::max(z[i].written, wp);
      else
        need(z[i].written <= capacity(i), "finished zone accounting");
    }
    for (size_t i = o.meta; i < z.size(); i++)
      if (z[i].finished && dev.g[i].state != 3) {
        dev.command(i, 'f');
        c.finishes++;
      }
    recovering = false;
    reset_zero();
    log("recover", "", -1, cp.size());
  }
  // Cache the same aggregate over every written extent, including deleted
  // extents until RESET. Append is O(1); promotion and relocation rebuild only
  // affected zones. This changes bookkeeping cost, never the placement rule.
  void merge_hint(int zi, const Checkpoint &q) {
    auto &v = z[zi].hints;
    v[0] = std::min(v[0], q.death);
    v[1] = std::max(v[1], q.death);
    v[2] = std::min(v[2], q.death >= INF
                              ? INF
                              : (q.death > q.epoch ? q.death - q.epoch : U(1)));
    v[3] = std::min(v[3], U(q.cl));
    v[4] = q.id;
  }
  void add_hint(int zi, const Checkpoint &q) {
    U t = now_ns();
    merge_hint(zi, q);
    c.hint_update += now_ns() - t;
  }
  void rebuild_hint(int zi) {
    U t = now_ns();
    z[zi].hints = {INF, 0, INF, INF, 0};
    for (auto &e : ex)
      if (e.z == zi && e.n)
        merge_hint(zi, cp.at(e.key));
    c.hint_update += now_ns() - t;
  }
  std::vector<std::array<U, 5>> all_hints() const {
    std::vector<std::array<U, 5>> h;
    h.reserve(z.size());
    for (auto &zone : z)
      h.push_back(zone.hints);
    return h;
  }

  void finish(int zi) {
    sample();
    Rec r = rec('F');
    r.a = zi;
    r.b = z[zi].written;
    journal({r});
    fault("afterfinishjournal", "");
    z[zi].finished = true;
    dev.command(zi, 'f');
    c.finishes++;
    log("finish", "", zi, capacity(zi) - z[zi].written);
    sample();
  }
  unsigned data_active_limit() const {
    return unsigned(o.maxactive - (o.zenfs_resources ? 2 : 1));
  }
  unsigned data_open_limit() const {
    return unsigned(o.maxopen - (o.zenfs_resources ? 3 : 1));
  }
  int choose_zenfs(const std::string &key, U reserve_remaining) {
    U started = now_ns();
    std::vector<zenfs_allocator::Zone> states;
    std::vector<U> live(z.size());
    for (auto &e : ex)
      if (e.live)
        live[e.z] += e.n;
    U empty = 0;
    for (size_t i = o.meta; i < z.size(); i++)
      empty += dev.g[i].state == 0;
    for (size_t i = o.meta; i < z.size(); i++) {
      auto &g = dev.g[i];
      bool fresh = g.state == 0;
      states.push_back(
          {g.state == 3 ? 0 : capacity(i) - z[i].written, capacity(i), live[i],
           z[i].lifetime, fresh,
           fresh && !o.no_empty_reserve && empty <= 1 &&
               (!reserve_remaining || reserve_remaining > capacity(i))});
    }
    U finish_ns = 0;
    int selected = zenfs_allocator::allocate(
        states, cp.at(key).cl + 2, data_active_limit(), 0, [&](int i) {
          U t = now_ns();
          finish(i + o.meta);
          finish_ns += now_ns() - t;
        });
    if (selected >= 0) {
      selected += o.meta;
      z[selected].lifetime = states[selected - o.meta].lifetime;
    }
    c.alloc += now_ns() - started - finish_ns;
    return selected;
  }
  void activate(int zi) {
    if (dev.g[zi].state == 1)
      return;
    U opened = 0, active = 0;
    for (size_t i = o.meta; i < z.size(); i++) {
      opened += dev.g[i].state == 1;
      active += dev.g[i].state == 1 || dev.g[i].state == 2;
    }
    if (dev.g[zi].state == 0 && active >= U(data_active_limit())) {
      int best = -1;
      for (size_t i = o.meta; i < z.size(); i++)
        if (int(i) != zi && (dev.g[i].state == 1 || dev.g[i].state == 2))
          if (best < 0 ||
              capacity(i) - z[i].written < capacity(best) - z[best].written)
            best = int(i);
      need(best >= 0, "no finish victim");
      bool wasopen = dev.g[best].state == 1;
      finish(best);
      if (wasopen)
        opened--;
    }
    if (opened >= U(data_open_limit())) {
      int best = -1;
      for (size_t i = o.meta; i < z.size(); i++)
        if (dev.g[i].state == 1 && (best < 0 || z[i].last < z[best].last))
          best = int(i);
      need(best >= 0, "no close victim");
      dev.command(best, 'c');
      c.closes++;
      log("close", "", best, 0);
    }
    dev.command(zi, 'o');
    sample();
  }
  int choose(const std::string &key, U left, bool relaxed = false,
             U reserve_remaining = 0) {
    if (policy == "zenfs_alloc")
      return choose_zenfs(key, reserve_remaining);
    U t = now_ns();
    auto &q = cp.at(key);
    auto hs = all_hints();
    int best = -1;
    long double bestscore = 0;
    U empty = 0, active = 0;
    for (size_t i = o.meta; i < z.size(); i++) {
      empty += dev.g[i].state == 0;
      active += dev.g[i].state == 1 || dev.g[i].state == 2;
    }
    for (size_t i = o.meta; i < z.size(); i++) {
      auto &g = dev.g[i];
      bool fresh = g.state == 0;
      if (relaxed && fresh)
        continue;
      if (g.state == 3 ||
          (fresh && !o.no_empty_reserve && empty <= 1 &&
           (!reserve_remaining || reserve_remaining > capacity(i))))
        continue;
      if (!fresh && z[i].written >= capacity(i))
        continue;
      auto h = hs[i];
      U n = std::min(left, capacity(i) - z[i].written);
      bool ok = fresh;
      if (!fresh) {
        if (policy == "fifo")
          ok = true;
        else if (policy == "isolation")
          ok = h[4] == q.id;
        else if (policy == "coarse")
          ok = (relaxed || h[3] == U(q.cl)) &&
               ((h[1] >= INF) == (q.death >= INF));
        else if (o.mix_anchors || (h[1] >= INF) == (q.death >= INF)) {
          if (q.death >= INF || h[1] >= INF || relaxed)
            ok = true;
          else {
            U horizon = q.death > q.epoch ? q.death - q.epoch : 1;
            U span =
                o.adaptive
                    ? std::max<U>(1, std::min(h[2], horizon) /
                                         ((o.resource_adaptive &&
                                           active >= U(data_active_limit()))
                                              ? 1
                                              : 2))
                    : o.window - 1;
            // A short span can strand most of a zone for sub-zone generations.
            // Admit enough alpha-sized expiry steps to fill one zone at the
            // incoming generation's density. The original DBT/tail score still
            // selects a zone.
            if (o.adaptive && q.stored < capacity(i) / 2) {
              U slots = (capacity(i) + q.stored - 1) / q.stored;
              U step = std::max<U>(1, std::min(h[2], horizon) / 4);
              span = std::max(span, (slots - 1) * step);
            }
            ok = std::max(h[1], q.death) - std::min(h[0], q.death) <= span;
          }
        }
      }
      if (!ok)
        continue;
      long double score = 0;
      if (policy == "retainz") {
        U d = q.death >= INF ? epoch + 64 : q.death;
        U old = fresh ? d : (h[1] >= INF ? epoch + 64 : h[1]);
        U D = std::max(d, old);
        long double delta =
            (long double)n * (D - d) + (long double)z[i].written * (D - old);
        if (o.nearest)
          delta = (long double)n * (old > d ? old - d : d - old);
        long double alpha =
            o.adaptive
                ? std::max<long double>(
                      1, (q.death >= INF
                              ? 1
                              : (q.death > q.epoch ? q.death - q.epoch : 1)) *
                             .25L)
                : 1;
        score = delta + alpha * (capacity(i) - z[i].written - n);
      } else
        score = fresh ? 1e30L : (long double)z[i].last;
      if (best < 0 || score < bestscore ||
          (score == bestscore && z[i].erases < z[best].erases)) {
        best = int(i);
        bestscore = score;
      }
    }
    c.alloc += now_ns() - t;
    return best;
  }
  // Admission runs only after the policy and copying GC have no candidate.
  // Reserve masking can leave an upstream allocator with a spare active token
  // but no fresh zone. Complete this transaction through compatible tails,
  // then lend the last empty zone. Check the entire padded remainder first.
  int choose_admission(const std::string &key, U remaining) {
    auto &q = cp.at(key);
    U available = 0;
    int tail = -1, empty = -1;
    for (size_t i = o.meta; i < z.size(); i++) {
      if (dev.g[i].state == 3)
        continue;
      if (dev.g[i].state == 0) {
        if (empty < 0)
          empty = int(i);
        continue;
      }
      if ((z[i].hints[1] >= INF) != (q.death >= INF))
        continue;
      U free = capacity(i) - z[i].written;
      available += free;
      if (free && (tail < 0 || free < capacity(tail) - z[tail].written))
        tail = int(i);
    }
    if (empty >= 0)
      available += capacity(empty);
    if (remaining > available)
      return -1;
    if (tail >= 0)
      return tail;
    if (empty >= 0 && remaining <= capacity(empty)) {
      z[empty].lifetime = q.cl + 2;
      c.reserve_borrows++;
      c.reserve_borrow_bytes += remaining;
      log("borrow_reserve", key, empty, remaining);
      return empty;
    }
    return -1;
  }
  void reset_zone(int zi) {
    sample();
    for (auto &e : ex)
      need(!(e.z == zi && e.live), "reset would erase live extent");
    Rec r = rec('Z');
    r.a = zi;
    journal({r});
    dev.command(zi, 'r');
    U t = now_ns();
    for (auto &e : ex)
      if (e.z == zi && e.n && e.deleted_ns)
        delays.push_back({t - e.deleted_ns, e.n, epoch - e.deleted_epoch});
    apply({r});
    c.resets++;
    log("reset", "", zi, 0);
    sample();
  }
  void reset_zero() {
    std::vector<U> live(z.size());
    for (auto &e : ex)
      if (e.live)
        live[e.z] += e.n;
    for (size_t i = o.meta; i < z.size(); i++)
      if (dev.g[i].state != 0 && live[i] == 0)
        reset_zone(i);
  }
  bool gc(const std::string &incoming) {
    U start = now_ns();
    std::vector<U> live(z.size());
    std::vector<bool> pending(z.size());
    for (auto &e : ex)
      if (e.live) {
        live[e.z] += e.n;
        if (!cp.at(e.key).committed)
          pending[e.z] = true;
      }
    auto hs = all_hints();
    std::vector<int> eligible;
    for (size_t i = o.meta; i < z.size(); i++)
      if (live[i] && live[i] < z[i].written && !pending[i])
        eligible.push_back(int(i));
    std::sort(eligible.begin(), eligible.end(),
              [&](int a, int b) { return live[a] < live[b]; });
    int dest = -1;
    std::vector<int> victims;
    long long bestgain = -999;
    long double bestbenefit = -1;
    // Pack several victims into one destination. Reusing an existing tail frees
    // a whole zone; evacuating two victims into the reserve creates a second
    // empty zone, allowing normal allocation to retain its GC reserve.
    for (size_t di = o.meta; di < z.size(); di++) {
      if (dev.g[di].state == 3 || pending[di])
        continue;
      U avail = capacity(di) - z[di].written;
      if (!avail)
        continue;
      for (int seed : eligible) {
        if (seed == int(di) || live[seed] > avail)
          continue;
        std::vector<int> plan;
        U copied = 0, garbage = 0;
        auto base = hs[seed];
        bool dstempty = dev.g[di].state == 0;
        auto compatible = [&](int vi) {
          auto h = hs[vi];
          if ((policy == "retainz" && !o.mix_anchors) ||
              (policy == "coarse" && o.pressure_fallback) ||
              policy == "zenfs_alloc")
            return (base[1] >= INF) == (h[1] >= INF);
          if (policy == "coarse")
            return base[3] == h[3] && ((base[1] >= INF) == (h[1] >= INF));
          return true;
        };
        if (!dstempty) {
          auto h = hs[di];
          if (((policy == "retainz" && !o.mix_anchors) ||
               (policy == "coarse" && o.pressure_fallback) ||
               policy == "zenfs_alloc") &&
              (base[1] >= INF) != (h[1] >= INF))
            continue;
          if (policy == "coarse" && !o.pressure_fallback &&
              (base[3] != h[3] || ((base[1] >= INF) != (h[1] >= INF))))
            continue;
        }
        plan.push_back(seed);
        copied = live[seed];
        garbage = z[seed].written - live[seed];
        for (int vi : eligible)
          if (vi != seed && vi != int(di) && compatible(vi) &&
              copied + live[vi] <= avail) {
            plan.push_back(vi);
            copied += live[vi];
            garbage += z[vi].written - live[vi];
          }
        long long gain = (long long)plan.size() - (dstempty ? 1 : 0);
        long double benefit = (long double)garbage / copied;
        if (gain > bestgain || (gain == bestgain && benefit > bestbenefit)) {
          dest = int(di);
          victims = plan;
          bestgain = gain;
          bestbenefit = benefit;
        }
      }
    }
    if (dest < 0)
      return false;
    if (dev.g[dest].state == 0) {
      for (auto &e : ex)
        if (e.live && e.z == victims.front()) {
          z[dest].lifetime = cp.at(e.key).cl + 2;
          break;
        }
    }
    sample();
    activate(dest);
    std::vector<Rec> moves;
    U off = z[dest].written, copied = 0;
    for (int victim : victims)
      for (size_t ei = 0; ei < ex.size(); ei++) {
        auto &e = ex[ei];
        if (e.z != victim || !e.live)
          continue;
        for (U p = 0; p < e.n; p += BUFSZ) {
          U n = std::min<U>(BUFSZ, e.n - p);
          U t = now_ns();
          dev.read(victim, e.off + p, buf, n);
          c.read += n;
          dev.write(dest, off + p, buf, n);
          c.write_ns += now_ns() - t;
          c.gcwrite += n;
        }
        Rec r = rec('M', e.key);
        r.a = victim;
        r.b = e.off;
        r.c = dest;
        r.d = off;
        r.e = e.n;
        r.f = e.logical;
        r.g = z[dest].lifetime;
        moves.push_back(r);
        off += e.n;
        copied += e.n;
      }
    z[dest].written = off;
    z[dest].last = ++tick;
    sync();
    fault("aftercopy", incoming);
    journal(moves);
    fault("afterrelocate", incoming);
    apply(moves);
    sample();
    for (int victim : victims)
      reset_zone(victim);
    c.gc_ns += now_ns() - start;
    log("gc", "", dest, copied);
    return true;
  }

  void fault(const std::string &where, const std::string &key) {
    if (o.fault == where && (o.faultkey.empty() || o.faultkey == key)) {
      std::cerr << "INJECTED_CRASH " << where << " " << key << "\n";
      _exit(86);
    }
  }
  void create(const std::string &key, U death, int cl, U size,
              const std::vector<U> &shards) {
    need(size > 0, "empty checkpoint not supported");
    need(!cp.count(key), "checkpoint key cannot be reused");
    if (o.cadence_ms && last_checkpoint_start) {
      U target = last_checkpoint_start + o.cadence_ms * 1000000;
      U current = now_ns();
      if (current < target)
        std::this_thread::sleep_for(std::chrono::nanoseconds(target - current));
    }
    sample();
    U start = now_ns();
    last_checkpoint_start = start;
    Counters before = c;
    Checkpoint q;
    q.id = nextid++;
    q.epoch = epoch;
    q.death = death;
    q.cl = cl;
    q.size = size;
    q.committed = false;
    q.nshards = shards.size();
    U total = 0;
    for (U n : shards)
      total += align_up(n);
    q.stored = total;
    if (payload_fd >= 0) {
      need(payload_offsets.count(key), "checkpoint missing from payload index");
      q.payload_start = payload_offsets.at(key);
    }
    cp[key] = q;
    U logical = 0;
    std::vector<Rec> records;
    Rec b = rec('B', key);
    b.a = q.id;
    b.b = epoch;
    b.c = death;
    b.d = size;
    b.e = cl;
    b.f = total;
    b.g = shards.size();
    b.h = q.payload_start;
    records.push_back(b);
    // Pending checkpoint extents are kept live for allocation/GC safety but are
    // absent from the durable journal until the final transaction commits.
    bool admission_mode = false;
    for (U shard : shards) {
      U shard_end = logical + align_up(shard);
      while (logical < shard_end) {
        bool fallback = false;
        auto allocate = [&]() {
          fallback = false;
          if (admission_mode)
            return choose_admission(key, total - logical);
          int found = choose(key, shard_end - logical);
          if (found < 0 && o.pressure_fallback) {
            found = choose(key, shard_end - logical, true);
            fallback = found >= 0;
          }
          return found;
        };
        int zi = allocate();
        if (zi < 0) {
          bool reclaimed = gc(key);
          if (reclaimed)
            zi = allocate();
          // Admission first drains compatible tails. The final empty zone is
          // borrowed only when the whole padded remainder fits there. This is
          // admission, never permission to delete: explicit durable retirement
          // still follows.
          if (zi < 0 && o.borrow_reserve) {
            zi = choose_admission(key, total - logical);
            if (zi >= 0) {
              fallback = false;
              admission_mode = true;
            }
          }
          need(zi >= 0, "ENOSPC: no eligible zone or reclaimable GC victim");
        }
        activate(zi);
        U n = std::min(shard_end - logical, capacity(zi) - z[zi].written),
          off = z[zi].written;
        sample();
        for (U p = 0; p < n; p += BUFSZ) {
          U len = std::min<U>(BUFSZ, n - p);
          if (payload_fd >= 0)
            all_io(payload_fd, buf, len, q.payload_start + logical + p, false);
          else
            fill_pattern(buf, len, q.id, logical + p);
          U t = now_ns();
          dev.write(zi, off + p, buf, len);
          c.write_ns += now_ns() - t;
          c.payload += len;
        }
        Ext e;
        e.key = key;
        e.z = zi;
        e.off = off;
        e.n = n;
        e.logical = logical;
        cp[key].ext.push_back(ex.size());
        ex.push_back(e);
        add_hint(zi, cp.at(key));
        z[zi].written += n;
        z[zi].last = ++tick;
        if (fallback) {
          c.fallbacks++;
          c.fallback_bytes += n;
        }
        Rec er = rec('E', key);
        er.a = zi;
        er.b = off;
        er.c = n;
        er.d = logical;
        er.e = z[zi].lifetime;
        records.push_back(er);
        logical += n;
        sample();
        log("write", key, zi, n);
      }
      if (o.zenfs_resources) {
        for (size_t i = o.meta; i < z.size(); i++)
          if (dev.g[i].state == 1) {
            dev.command(int(i), 'c');
            c.closes++;
            log("close", key, int(i), 0);
          }
      }
      c.shards++;
    }
    sync();
    fault("afterwrite", key);
    if (o.fault == "beforecommit" &&
        (o.faultkey.empty() || o.faultkey == key)) {
      journal(records, false);
      fault("beforecommit", key);
    }
    journal(records);
    cp[key].committed = true;
    c.logical += size;
    c.commits++;
    fault("aftercommit", key);
    if (policy == "isolation") {
      for (auto i : cp[key].ext) {
        int zi = ex[i].z;
        if (dev.g[zi].state == 1 || dev.g[zi].state == 2)
          finish(zi);
      }
    }
    log("commit", key, -1, size);
    row("C", key, "ok", (now_ns() - start) / 1e9, 0, 0, 0, true);
    (void)before;
  }
  void erase(const std::string &key) {
    need(cp.count(key) && cp[key].live && cp[key].committed,
         "delete requires live committed checkpoint");
    sample();
    Rec r = rec('D', key);
    r.a = now_ns();
    r.b = epoch;
    journal({r});
    apply({r});
    sample();
    reset_zero();
    log("delete", key, -1, 0);
    row("D", key, "ok");
  }
  void promote(const std::string &key, U death) {
    need(cp.count(key) && cp[key].live && cp[key].committed,
         "promotion requires live checkpoint");
    need(death >= cp[key].death, "promotion cannot shorten retention hint");
    Rec r = rec('P', key);
    r.a = death;
    journal({r});
    apply({r});
    log("promote", key, -1, death);
    row("P", key, "ok");
  }
  void restore(const std::string &key) {
    need(cp.count(key) && cp[key].live && cp[key].committed,
         "restore requires live committed checkpoint");
    auto &q = cp.at(key);
    U io = 0, verify = 0, nread = 0;
    for (auto i : q.ext) {
      auto &e = ex[i];
      need(e.live, "restore encountered deleted extent");
      for (U p = 0; p < e.n; p += BUFSZ) {
        U n = std::min<U>(BUFSZ, e.n - p);
        U t = now_ns();
        dev.read(e.z, e.off + p, buf, n);
        io += now_ns() - t;
        c.read += n;
        t = now_ns();
        if (payload_fd >= 0) {
          all_io(payload_fd, verifybuf, n, q.payload_start + e.logical + p,
                 false);
          need(memcmp(buf, verifybuf, n) == 0,
               "restored actual payload mismatch");
        } else
          need(verify_pattern(buf, n, q.id, e.logical + p),
               "restored payload mismatch");
        verify += now_ns() - t;
        if (restore_fd >= 0)
          all_io(restore_fd, buf, n, q.payload_start + e.logical + p, true);
        nread += n;
      }
    }
    need(nread == q.stored, "restore length mismatch");
    if (restore_fd >= 0)
      sysneed(fsync(restore_fd) == 0, "restore output fsync");
    row("R", key, "ok", 0, io / 1e9, verify / 1e9, q.size, true);
    log("restore", key, -1, q.size);
  }
  void row(const std::string &ev, const std::string &key,
           const std::string &status, double seconds = 0, double rio = 0,
           double verify = 0, U restored = 0, bool verified = false) {
    sample();
    auto s = state();
    U mx = 0;
    long double mean = 0, variance = 0;
    for (size_t i = o.meta; i < z.size(); i++) {
      mx = std::max(mx, z[i].erases);
      mean += z[i].erases;
    }
    mean /= z.size() - o.meta;
    for (size_t i = o.meta; i < z.size(); i++)
      variance += (z[i].erases - mean) * (z[i].erases - mean);
    double cv =
        mean ? std::sqrt(double(variance / (z.size() - o.meta))) / double(mean)
             : 0;
    U mem = z.capacity() * sizeof(Zone) + ex.capacity() * sizeof(Ext) +
            cp.size() * sizeof(Checkpoint);
    for (auto &it : cp)
      mem += it.second.ext.capacity() * sizeof(size_t) + it.first.capacity();
    struct rusage ru{};
    sysneed(getrusage(RUSAGE_SELF, &ru) == 0, "getrusage");
    U rss = ru.ru_maxrss;
#ifndef __APPLE__
    rss *= 1024;
#endif
    csv << ev << ',' << epoch << ',' << key << ',' << status << ',' << seconds
        << ',' << c.alloc << ',' << c.write_ns << ',' << c.gc_ns << ','
        << c.sync_ns << ',' << c.logical << ',' << c.payload << ',' << c.gcwrite
        << ',' << c.meta << ',' << c.read << ',' << s.invalid << ',' << s.tail
        << ',' << s.stranded << ',' << s.live << ',' << s.free << ',' << s.empty
        << ',' << s.open << ',' << s.active << ',' << c.peakopen << ','
        << c.peakactive << ',' << c.peaklive << ',' << c.peakreserved << ','
        << (double)dbt << ',' << c.resets << ',' << mx << ',' << cv << ','
        << rio << ',' << verify << ',' << restored << ',' << verified << ','
        << recovery_s << ',' << mem << ',' << c.shards << ',' << rss << ','
        << c.fallbacks << ',' << c.fallback_bytes << ',' << c.reserve_borrows
        << ',' << c.reserve_borrow_bytes << ',' << c.hint_update << '\n';
    csv.flush();
    events.flush();
  }
  void finish_run(bool ok = true) {
    if (ok)
      row("Q", "", "ok");
    std::ofstream zs(outprefix + ".zones.csv"),
        ds(outprefix + ".reclamation.csv");
    zs << "zone,capacity,written_bytes,live_bytes,wp,state,resets,lifetime_"
          "hint\n";
    for (size_t i = o.meta; i < z.size(); i++) {
      U live = 0;
      for (auto &e : ex)
        if (e.z == int(i) && e.live)
          live += e.n;
      zs << i << ',' << capacity(i) << ',' << z[i].written << ',' << live << ','
         << dev.g[i].wp << ',' << dev.g[i].state << ',' << z[i].erases << ','
         << z[i].lifetime << '\n';
    }
    ds << "delay_ns,bytes,delay_epochs,censored,delete_epoch_stamped\n";
    for (auto &d : delays)
      ds << d[0] << ',' << d[1] << ',' << d[2] << ",0,1\n";
    U end = now_ns();
    for (auto &e : ex)
      if (!e.live && e.n && e.deleted_ns)
        ds << end - e.deleted_ns << ',' << e.n << ',' << epoch - e.deleted_epoch
           << ",1,1\n";
    std::cout << "RESULT {\"backend\":\"online\",\"policy\":\"" << policy
              << "\",\"status\":\"" << (ok ? "ok" : "failed")
              << "\",\"committed_checkpoints\":" << c.commits
              << ",\"failed_epoch\":" << (ok ? 0 : epoch)
              << ",\"logical_bytes\":" << c.logical
              << ",\"payload_bytes\":" << c.payload
              << ",\"gc_write_bytes\":" << c.gcwrite
              << ",\"metadata_write_bytes\":" << c.meta
              << ",\"resets\":" << c.resets
              << ",\"recover\":" << (o.recover ? "true" : "false")
              << ",\"mock_device\":" << (dev.mock ? "true" : "false") << "}\n";
  }
};
static U number(const std::string &s) {
  size_t n = 0;
  U v = std::stoull(s, &n);
  need(n == s.size(), "invalid number");
  return v;
}
static U death(const std::string &s) {
  return s == "inf" || s == "INF" ? INF : number(s);
}
static int life(const std::string &s) {
  if (s == "short")
    return 0;
  if (s == "medium")
    return 1;
  if (s == "long")
    return 2;
  if (s == "extreme")
    return 3;
  throw std::runtime_error("unknown lifetime class");
}
int main(int argc, char **argv) {
  try {
    if (argc < 6) {
      std::cerr << "usage: online_bench device trace.tsv policy zone_bytes "
                   "output_prefix [--file-zones N --meta-zones N --max-open N "
                   "--max-active N --recover --window N --adaptive "
                   "--zenfs-resources --no-empty-reserve --payload-image PATH "
                   "--payload-index PATH --restore-image PATH --fault "
                   "afterwrite|beforecommit|aftercommit|aftercopy|"
                   "afterrelocate|afterfinishjournal --fault-key KEY]\n";
      return 2;
    }
    Options o;
    for (int i = 6; i < argc; i++) {
      std::string a = argv[i];
      if (a == "--no-empty-reserve")
        o.no_empty_reserve = true;
      else if (a == "--zenfs-resources")
        o.zenfs_resources = true;
      else if (a == "--recover")
        o.recover = true;
      else if (a == "--adaptive")
        o.adaptive = true;
      else if (a == "--nearest")
        o.nearest = true;
      else if (a == "--mix-anchors")
        o.mix_anchors = true;
      else if (a == "--borrow-reserve")
        o.borrow_reserve = true;
      else if (a == "--pressure-fallback")
        o.pressure_fallback = true;
      else if (a == "--resource-adaptive") {
        o.adaptive = true;
        o.resource_adaptive = true;
      } else {
        need(i + 1 < argc, "option needs value");
        std::string v = argv[++i];
        if (a == "--payload-image")
          o.payload_image = v;
        else if (a == "--payload-index")
          o.payload_index = v;
        else if (a == "--restore-image")
          o.restore_image = v;
        else if (a == "--file-zones")
          o.filezones = number(v);
        else if (a == "--meta-zones" || a == "--data-zone-start")
          o.meta = number(v);
        else if (a == "--max-open")
          o.maxopen = number(v);
        else if (a == "--max-active")
          o.maxactive = number(v);
        else if (a == "--window")
          o.window = number(v);
        else if (a == "--cadence-ms")
          o.cadence_ms = number(v);
        else if (a == "--fault")
          o.fault = v;
        else if (a == "--fault-key")
          o.faultkey = v;
        else
          throw std::runtime_error("unknown option: " + a);
      }
    }
    need(o.fault.empty() || o.fault == "afterwrite" ||
             o.fault == "beforecommit" || o.fault == "aftercommit" ||
             o.fault == "aftercopy" || o.fault == "afterrelocate" ||
             o.fault == "afterfinishjournal",
         "unknown fault point");
    std::ifstream in(argv[2]);
    need(in.good(), "open trace");
    Backend b(argv[1], number(argv[4]), argv[3], argv[5], o);
    std::string line;
    U lineno = 0;
    bool quit = false;
    while (std::getline(in, line)) {
      lineno++;
      if (line.empty() || line[0] == '#')
        continue;
      std::istringstream s(line);
      std::string op, key, ds, cl, extra;
      U ep = 0, size = 0;
      s >> op;
      if (op == "Q") {
        need(!(s >> extra), "Q takes no fields");
        quit = true;
        break;
      }
      need(bool(s >> ep >> key),
           "invalid trace at line " + std::to_string(lineno));
      need(ep >= b.epoch, "trace epoch must not go backwards");
      need(key.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRS"
                                 "TUVWXYZ0123456789_.-") == std::string::npos,
           "key contains unsupported characters");
      b.epoch = ep;
      try {
        if (op == "C") {
          need(bool(s >> ds >> cl >> size),
               "C needs epoch key death class size_bytes [shard_sizes_csv]");
          std::vector<U> shards;
          if (s >> extra) {
            std::istringstream sh(extra);
            std::string num;
            U sum = 0;
            while (std::getline(sh, num, ',')) {
              U n = number(num);
              need(n > 0, "zero-length shard");
              shards.push_back(n);
              sum += n;
            }
            need(sum == size, "shard sizes must sum to size_bytes");
            need(!(s >> extra), "C extra fields");
          } else
            shards.push_back(size);
          b.create(key, death(ds), life(cl), size, shards);
        } else if (op == "D") {
          need(!(s >> extra), "D has extra fields");
          b.erase(key);
        } else if (op == "P") {
          need(bool(s >> ds) && !(s >> extra), "P needs newdeath");
          b.promote(key, death(ds));
        } else if (op == "R") {
          need(!(s >> extra), "R has extra fields");
          b.restore(key);
        } else
          throw std::runtime_error("unknown trace opcode: " + op);
      } catch (const std::exception &e) {
        b.row(op, key, "failed");
        b.finish_run(false);
        throw;
      }
    }
    need(quit, "trace must end with Q");
    b.finish_run();
    return 0;
  } catch (const std::exception &e) {
    std::cerr << "ERROR " << e.what() << "\n";
    return 1;
  }
}
