CXX ?= c++
CXXFLAGS ?= -O2 -std=c++17
LDFLAGS ?=
PYTHON ?= python3
SOURCES := $(wildcard src/*.cpp)
BENCHMARKS := $(patsubst src/%.cpp,build/%,$(SOURCES))

.PHONY: all benchmarks test analyze figures
all: build/retainz
benchmarks: $(BENCHMARKS) build/femu_codec
build/%: src/%.cpp src/zenfs_allocator.hpp
	@mkdir -p build
	$(CXX) $(CXXFLAGS) -I src $< -o $@ $(LDFLAGS)
build/femu_codec: tests/femu_codec.cpp
	@mkdir -p build
	$(CXX) $(CXXFLAGS) $< -o $@ $(LDFLAGS)
test: build/retainz build/placement_reference build/history_scan build/fixed_reserve
	$(PYTHON) tests/backend.py
	$(PYTHON) tests/placement.py
	$(PYTHON) tests/cached_summary.py
	$(PYTHON) tests/zenfs_allocator.py
analyze:
	$(PYTHON) tools/analyze.py --check-paper
figures: analyze
	$(PYTHON) tools/plot_pressure.py
	$(PYTHON) tools/plot_mechanisms.py
