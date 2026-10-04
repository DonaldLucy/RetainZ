// Adapted from ZenFS 919c2ebbcdc170525a9abffb8b61a3795b1e6ae5,
// fs/zbd_zenfs.cc. Copyright Facebook, Inc. and its affiliates;
// Copyright 2019-present Western Digital Corporation.
// Apache-2.0: see third_party/zenfs/LICENSE.Apache.
// This is the serial non-WAL allocation policy, not the ZenFS filesystem.
#pragma once
#include <cstdint>
#include <vector>

namespace zenfs_allocator {
struct Zone {
  uint64_t remaining = 0, maximum = 0, used = 0;
  int lifetime = 0; // RocksDB values: NOT_SET=0, NONE=1, SHORT=2,...EXTREME=5.
  bool empty = true, busy = false;
};

inline unsigned lifetime_diff(int zone, int file) {
  if (file == 0 || file == 1)
    return zone == file ? 0 : 100;
  if (zone > file)
    return unsigned(zone - file);
  if (zone == file)
    return 50;
  return 100;
}

inline int best_match(const std::vector<Zone> &zones, int hint, unsigned &diff,
                      uint32_t min_capacity = 0) {
  int selected = -1;
  diff = 100;
  for (size_t i = 0; i < zones.size(); ++i) {
    const auto &z = zones[i];
    if (!z.busy && z.used > 0 && z.remaining > 0 &&
        z.remaining >= min_capacity) {
      auto d = lifetime_diff(z.lifetime, hint);
      // Upstream uses <=: the LAST equal candidate wins.
      if (d <= diff) {
        selected = int(i);
        diff = d;
      }
    }
  }
  return selected;
}

// finish(i) executes the backend's durable FINISH before this state is updated.
// Resource reservations and GC belong to the common backend. The caller passes
// its usable data-zone active budget and marks reserved empty zones busy.
template <class Finish>
int allocate(std::vector<Zone> &zones, int hint, unsigned active_limit,
             unsigned finish_threshold, Finish finish) {
  auto finish_zone = [&](int i) {
    finish(i);
    zones[i].remaining = 0;
  };
  if (finish_threshold) {
    for (size_t i = 0; i < zones.size(); ++i) {
      auto &z = zones[i];
      if (!z.busy && !z.empty && z.remaining > 0 &&
          z.remaining < z.maximum * finish_threshold / 100)
        finish_zone(int(i));
    }
  }
  unsigned active = 0;
  for (const auto &z : zones)
    active += !z.empty && z.remaining > 0;
  unsigned diff;
  int selected = best_match(zones, hint, diff);
  if (diff >= 50) {
    bool got_token = active < active_limit;
    if (selected >= 0 && !(!got_token && diff == 50))
      selected = -1;
    if (selected < 0) {
      while (!got_token && active >= active_limit) {
        int victim = -1;
        for (size_t i = 0; i < zones.size(); ++i) {
          auto &z = zones[i];
          if (!z.busy && !z.empty && z.remaining > 0 &&
              (victim < 0 || zones[victim].remaining > z.remaining))
            victim = int(i);
        }
        // Upstream waits if all candidates are owned by concurrent writers.
        // The serial backend has no such owner; return unavailable, never spin.
        if (victim < 0)
          return -1;
        finish_zone(victim);
        --active;
      }
      for (size_t i = 0; i < zones.size(); ++i) {
        if (!zones[i].busy && zones[i].empty) {
          selected = int(i);
          zones[i].lifetime = hint;
          break;
        }
      }
    }
  }
  return selected;
}
} // namespace zenfs_allocator
