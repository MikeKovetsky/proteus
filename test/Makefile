# Makefile
# See https://docs.cocotb.org/en/stable/quickstart.html for more info

# defaults
SIM ?= icarus
FST ?= -fst # Use more efficient FST format
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../src
PROJECT_SOURCES = project.v protoemu_core.v protoemu_fifo.v protoemu_table.v

# The two table SRAMs are IHP's RM_IHPSG13_1P_512x32_c2_bm_bist macro. Its simulation model is vendored from
# IHP's open PDK (test/ihp_sram/README.md); FUNCTIONAL selects the model without timing checks.
SRAM_MODELS = $(PWD)/ihp_sram/RM_IHPSG13_1P_512x32_c2_bm_bist.v $(PWD)/ihp_sram/RM_IHPSG13_1P_core_behavioral_bm_bist.v
VERILOG_SOURCES += $(SRAM_MODELS)

ifneq ($(GATES),yes)

# RTL simulation:
SIM_BUILD				= sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))
COMPILE_ARGS    += -DFUNCTIONAL

else

# Gate level simulation:
SIM_BUILD				= sim_build/gl
COMPILE_ARGS    += -DGL_TEST
COMPILE_ARGS    += -DFUNCTIONAL
COMPILE_ARGS    += -DSIM
# Cell models for the PDK in $(PDK) (ihp-sg13cmos5l or ihp-sg13g2), e.g. sg13cmos5l_stdcell.v
PDK ?= ihp-sg13cmos5l
PDK_CELL_PREFIX = $(patsubst ihp-%,%,$(PDK))
PDK_LIBS_REF = $(PDK_ROOT)/$(PDK)/libs.ref
VERILOG_SOURCES += $(PDK_LIBS_REF)/$(PDK_CELL_PREFIX)_io/verilog/$(PDK_CELL_PREFIX)_io.v
# Newer PDK versions ship the UDP (user-defined primitive) models in a separate file
VERILOG_SOURCES += $(wildcard $(PDK_LIBS_REF)/$(PDK_CELL_PREFIX)_stdcell/verilog/$(PDK_CELL_PREFIX)_udp.v)
VERILOG_SOURCES += $(PDK_LIBS_REF)/$(PDK_CELL_PREFIX)_stdcell/verilog/$(PDK_CELL_PREFIX)_stdcell.v

# this gets copied in by the GDS action workflow
VERILOG_SOURCES += $(PWD)/gate_level_netlist.v

endif

# Allow sharing configuration between design and testbench via `include`:
COMPILE_ARGS 		+= -I$(SRC_DIR)

# Include the testbench sources:
VERILOG_SOURCES += $(PWD)/tb.v
TOPLEVEL = tb

# List test modules to run, separated by commas and without the .py suffix:
COCOTB_TEST_MODULES = test

# include cocotb's make rules to take care of the simulator setup
include $(shell cocotb-config --makefiles)/Makefile.sim

# ---- protoemu's own targets (after the include, so `make` alone still runs the cocotb test) ------------

.PHONY: lockstep mutants lint synth

# The golden model against the RTL core, clock for clock: 14 benches and 300 random tests (Verilator).
lockstep:
	$(MAKE) -C lockstep run

# Plant one bug at a time in the RTL; the lockstep suite must catch every one.
mutants: lockstep
	python3 lockstep/mutants.py

# Verilator lint of src/, every warning on. The vendored SRAM model's own warnings are waived.
# $(CURDIR), not the template's $(PWD), so that `make -C test lint` works from anywhere.
lint:
	verilator --lint-only -Wall --timescale 1ns/1ps -DFUNCTIONAL --top-module tt_um_mikekovetsky_protoemu \
		$(CURDIR)/lint/waivers.vlt $(addprefix $(CURDIR)/../src/,$(PROJECT_SOURCES)) \
		$(CURDIR)/ihp_sram/RM_IHPSG13_1P_512x32_c2_bm_bist.v $(CURDIR)/ihp_sram/RM_IHPSG13_1P_core_behavioral_bm_bist.v

# Yosys synthesis: generic cells with the SRAMs as black boxes, then IHP cells and the decision-loop delay.
synth:
	$(MAKE) -C synth
