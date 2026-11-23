# The lockstep suite: the golden model (bakeoff/cand_table) against the RTL core, clock for clock.
#
#   make -C test/lockstep              # build, record the 14 benches and 300 random tests, replay all
#   make -C test/lockstep RANDOM=1000  # more random tests
#   make -C test/lockstep SEED=500     # other random seeds
#
# Needs Verilator and a Python 3.9 or later (the OSS CAD Suite has both).

ROOT    := $(abspath ../..)
BUILD   := $(abspath build)
PY      ?= python3
RANDOM  ?= 300
CYCLES  ?= 4000
SEED    ?= 1
VERILATOR ?= verilator

RTL := $(ROOT)/src/proteus_core.v $(ROOT)/src/proteus_fifo.v $(ROOT)/src/proteus_table.v
SRAM := $(ROOT)/test/ihp_sram/RM_IHPSG13_1P_512x32_c2_bm_bist.v \
        $(ROOT)/test/ihp_sram/RM_IHPSG13_1P_core_behavioral_bm_bist.v

.PHONY: all bin run traces clean

all: run

bin: $(BUILD)/lockstep

# The PDK's SRAM model checks A_DLY after a #0 delay; Verilator runs without timing and says so (STMTDLY).
$(BUILD)/lockstep: lockstep.cpp $(RTL) $(SRAM)
	mkdir -p $(BUILD)/obj
	$(VERILATOR) --cc --exe --build -j 0 -O3 --no-timing -Wno-STMTDLY --timescale 1ns/1ps -DFUNCTIONAL \
		--top-module proteus_core --Mdir $(BUILD)/obj -o $(BUILD)/lockstep \
		$(ROOT)/test/lint/waivers.vlt $(RTL) $(SRAM) $(abspath lockstep.cpp)

traces:
	$(PY) traces.py --out $(BUILD)/traces --random $(RANDOM) --cycles $(CYCLES) --seed $(SEED)

run: $(BUILD)/lockstep traces
	$(BUILD)/lockstep $(BUILD)/traces/*.trace

clean:
	rm -rf $(BUILD)
