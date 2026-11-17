# Synthesis and a timing estimate, outside LibreLane.
#
#   make -C test/synth           # generic synthesis, IHP mapping and the decision-loop delay
#
# The first run fetches two liberty files from IHP's open PDK at a pinned commit (about 1.8 MB).
# Needs Yosys (the OSS CAD Suite) and Python 3.9 or later.

PDK_SHA  := e73adb4045731ddec21634146d536380057f6a26
PDK_RAW  := https://raw.githubusercontent.com/IHP-GmbH/IHP-Open-PDK/$(PDK_SHA)
STD_LIB  := build/sg13cmos5l_stdcell_slow_1p08V_125C.lib
SRAM_LIB := build/RM_IHPSG13_1P_512x32_c2_bm_bist_slow_1p08V_125C.lib
PY       ?= python3
YOSYS    ?= yosys
SRC      := $(wildcard ../../src/*.v)

.PHONY: all generic ihp timing clean

all: generic timing

$(STD_LIB):
	mkdir -p build
	curl -sSfL -o $@ $(PDK_RAW)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/lib/sg13cmos5l_stdcell_slow_1p08V_125C.lib

$(SRAM_LIB):
	mkdir -p build
	curl -sSfL -o $@ $(PDK_RAW)/ihp-sg13g2/libs.ref/sg13g2_sram/lib/RM_IHPSG13_1P_512x32_c2_bm_bist_slow_1p08V_125C.lib

# Generic cells; fails if any flop has no asynchronous reset (R4).
generic: $(SRC)
	mkdir -p build
	$(YOSYS) -q -l build/generic.log generic.ys
	@grep -E "cells|DFF|RM_IHPSG13" build/generic_stat.txt

build/ihp_netlist.json: $(SRC) $(STD_LIB) ihp.ys
	$(YOSYS) -q -l build/ihp.log ihp.ys

ihp: build/ihp_netlist.json
	@grep -E "cells|dfrbp|Chip area|sequential" build/ihp_stat.txt

timing: build/ihp_netlist.json $(SRAM_LIB)
	$(PY) loop_delay.py build/ihp_netlist.json $(STD_LIB) $(SRAM_LIB)

clean:
	rm -rf build
