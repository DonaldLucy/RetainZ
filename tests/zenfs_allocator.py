#!/usr/bin/env python3
"""Compile unchanged upstream function bodies against a serial state harness.

The oracle is extracted from the vendored, unpatched fixed revision. It does
not call the adapted policy. Compare decisions, FINISH order, tags, capacities,
and resource-token results. This validates the allocation policy, not ZenFS GC.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "third_party/zenfs/fs/zbd_zenfs.cc"


def function(source, signature):
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


PRE = r"""
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <iostream>
#include <memory>
#include <random>
#include <vector>
#include "src/zenfs_allocator.hpp"
namespace upstream {
struct Env {enum WriteLifeTimeHint{WLTH_NOT_SET,WLTH_NONE,WLTH_SHORT,WLTH_MEDIUM,WLTH_LONG,WLTH_EXTREME};static void* Default(){return nullptr;}};
enum class IOType{kWAL,kUnknown};
struct IOStatus{bool ok()const{return true;}static IOStatus OK(){return {};}};
template<class... T>void Debug(T...){ }
template<class... T>void Info(T...){ }
enum{ZENFS_WAL_IO_ALLOC_LATENCY,ZENFS_L0_IO_ALLOC_LATENCY,ZENFS_NON_WAL_IO_ALLOC_LATENCY,ZENFS_IO_ALLOC_QPS,ZENFS_OPEN_ZONES_COUNT,ZENFS_ACTIVE_ZONES_COUNT};
struct Metrics{template<class...T>void ReportQPS(T...){}template<class...T>void ReportGeneral(T...){} };
struct ZenFSMetricsLatencyGuard{template<class...T>ZenFSMetricsLatencyGuard(T...){} };
struct Zone{
 uint64_t capacity_,max_capacity_,used_capacity_,start_,wp_;
 Env::WriteLifeTimeHint lifetime_;bool empty,busy;int id;
 std::vector<int>* finished;
 bool Acquire(){if(busy)return false;busy=true;return true;}
 void Release(){assert(busy);busy=false;}
 IOStatus CheckRelease(){Release();return {};}
 bool IsBusy(){return busy;}bool IsEmpty(){return empty;}bool IsFull(){return capacity_==0;}
 IOStatus Finish(){finished->push_back(id);capacity_=0;return {};}
};
#define LIFETIME_DIFF_NOT_GOOD (100)
#define LIFETIME_DIFF_COULD_BE_WORSE (50)
struct ZonedBlockDevice {
 std::vector<Zone*> io_zones;std::vector<int>finished;
 unsigned active_io_zones_=0,open_io_zones_=0,max_nr_active_io_zones_=0,finish_threshold_=0;
 void*logger_=nullptr;Metrics storage;Metrics*metrics_=&storage;
 IOStatus GetZoneDeferredStatus(){return {};}
 void WaitForOpenIOZoneToken(bool){++open_io_zones_;}
 bool GetActiveIOZoneTokenIfAvailable(){if(active_io_zones_>=max_nr_active_io_zones_)return false;++active_io_zones_;return true;}
 void PutActiveIOZoneToken(){assert(active_io_zones_);--active_io_zones_;}
 void PutOpenIOZoneToken(){assert(open_io_zones_);--open_io_zones_;}
 void LogZoneStats(){}
 IOStatus ApplyFinishThreshold();IOStatus FinishCheapestIOZone();
 IOStatus GetBestOpenZoneMatch(Env::WriteLifeTimeHint,unsigned*,Zone**,uint32_t=0);
 IOStatus AllocateEmptyZone(Zone**);
 IOStatus AllocateIOZone(Env::WriteLifeTimeHint,IOType,Zone**);
};
"""
POST = r"""
} // upstream
using Z=zenfs_allocator::Zone;
static uint64_t cases=0;
void check(std::vector<Z> input,int hint,unsigned limit,unsigned threshold){
 upstream::ZonedBlockDevice oracle;oracle.max_nr_active_io_zones_=limit;oracle.finish_threshold_=threshold;
 std::vector<upstream::Zone> oz;
 unsigned active=0;for(auto&z:input)active+=!z.empty&&z.remaining>0;
 // Concurrent-owner wait is outside the serial checkpoint backend's inputs.
 if(active>=limit){bool releasable=false;for(auto&z:input)releasable|=!z.busy&&!z.empty&&z.remaining>0;if(!releasable)return;}
 oracle.active_io_zones_=active;
 for(size_t i=0;i<input.size();++i){auto&z=input[i];oz.push_back({z.remaining,z.maximum,z.used,uint64_t(i)*65536,z.empty?uint64_t(i)*65536:uint64_t(i)*65536+4096,upstream::Env::WriteLifeTimeHint(z.lifetime),z.empty,z.busy,int(i),&oracle.finished});}
 for(auto&z:oz)oracle.io_zones.push_back(&z);
 upstream::Zone*selected=nullptr;
 oracle.AllocateIOZone(upstream::Env::WriteLifeTimeHint(hint),upstream::IOType::kUnknown,&selected);
 std::vector<int>finished;
 int got=zenfs_allocator::allocate(input,hint,limit,threshold,[&](int i){finished.push_back(i);});
 int expected=selected?selected->id:-1;
 if(got!=expected||finished!=oracle.finished){std::cerr<<"decision mismatch case "<<cases<<" hint="<<hint<<" got="<<got<<" expected="<<expected<<"\n";std::exit(1);}
 for(size_t i=0;i<input.size();++i)assert(input[i].remaining==oz[i].capacity_&&input[i].lifetime==int(oz[i].lifetime_));
 unsigned expected_active=active-unsigned(finished.size())+(got>=0&&input[got].empty);
 assert(oracle.active_io_zones_==expected_active);
 assert(oracle.open_io_zones_==unsigned(got>=0));
 ++cases;
}
int main(){
 for(int z=0;z<6;++z)for(int f=0;f<6;++f)assert(zenfs_allocator::lifetime_diff(z,f)==upstream::GetLifeTimeDiff(upstream::Env::WriteLifeTimeHint(z),upstream::Env::WriteLifeTimeHint(f)));
 // Complete two-zone lifetime/capacity/active-limit decision grid.
 for(int a=0;a<6;++a)for(int b=0;b<6;++b)for(int hint=0;hint<6;++hint)
 for(unsigned limit:{1u,2u,3u})for(uint64_t ca:{0,4096,32768})for(uint64_t cb:{0,4096,32768})
 for(bool empty:{false,true})check({{ca,65536,4096,a,false,false},{cb,65536,4096,b,false,false},{65536,65536,0,0,true,!empty}},hint,limit,0);
 std::mt19937_64 rng(20260926);
 for(int trial=0;trial<60000;++trial){
  std::vector<Z> zones;unsigned active=0;
  int n=2+rng()%31;
  for(int i=0;i<n;++i){int state=rng()%3;bool empty=state==0;uint64_t remaining=empty?65536:(state==1?4096*(1+rng()%15):0);active+=state==1;
    zones.push_back({remaining,65536,empty?0:4096*(1+rng()%8),empty?0:int(rng()%6),empty,false});}
  unsigned limit=std::max(1u,active+unsigned(rng()%2));
  if(rng()%2)for(auto&z:zones)if(z.empty)z.busy=true; // common reserve masks
  check(zones,int(rng()%6),limit,trial%3?0:10);
 }
 std::cout<<"{\"all_pass\":true,\"lifetime_pairs\":36,\"allocation_states\":"<<cases<<",\"seed\":20260926}\n";
}
"""


def main():
    functions = [
        "unsigned int GetLifeTimeDiff(",
        "IOStatus ZonedBlockDevice::ApplyFinishThreshold()",
        "IOStatus ZonedBlockDevice::FinishCheapestIOZone()",
        "IOStatus ZonedBlockDevice::GetBestOpenZoneMatch(",
        "IOStatus ZonedBlockDevice::AllocateEmptyZone(",
        "IOStatus ZonedBlockDevice::AllocateIOZone(",
    ]
    source = SOURCE.read_text()
    out = ROOT / "build/zenfs_policy"
    out.mkdir(parents=True, exist_ok=True)
    generated = out / "differential.cpp"
    generated.write_text(
        PRE + "\n\n".join(function(source, f) for f in functions) + POST
    )
    binary = out / "differential"
    subprocess.run(
        [
            "c++",
            "-std=c++17",
            "-O2",
            "-I",
            str(ROOT),
            str(generated),
            "-o",
            str(binary),
        ],
        check=True,
    )
    result = json.loads(subprocess.check_output([str(binary)], text=True))
    result.update(
        upstream_revision="919c2ebbcdc170525a9abffb8b61a3795b1e6ae5",
        functions=functions,
        scope="Serial successful-I/O allocator decisions; not concurrent scheduling, native filesystem GC or error injection.",
    )
    dest = ROOT / "results/tests/zenfs"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "differential.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
