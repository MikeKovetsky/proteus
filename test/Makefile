# Makefile
# See https://docs.cocotb.org/en/stable/quickstart.html for more info

# defaults
SIM ?= icarus
FST ?= -fst # Use more efficient FST format
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../src
# The flow test (src/flowtest_*.v) stands in for the real RTL until it lands.
PROJECT_SOURCES = flowtest_top.v flowtest_lanes.v

# The IHP 512x32 SRAM macro's behavioural model, from the PDK's libs.ref/sg13g2_sram/verilog/ (IHP-Open-PDK
# 89d42ee6d2fd, the commit Tiny Tapeout's flow installs; Apache-2.0, "Copyright 2023 IHP PDK Authors"). It is
# vendored in ihp_sram/ because the GitHub test job has no PDK. The gate-level netlist keeps the macro as a
# black box too, so both builds need it. FUNCTIONAL selects the model's zero-delay behaviour.
# Ken Pettit's ihp-um-janestreet-prism does the same (test/test_basic.mk).
SRAM_MODEL_DIR ?= $(PWD)/ihp_sram
SRAM_MODELS = $(SRAM_MODEL_DIR)/RM_IHPSG13_1P_core_behavioral_bm_bist.v \
              $(SRAM_MODEL_DIR)/RM_IHPSG13_1P_512x32_c2_bm_bist.v
VERILOG_SOURCES += $(SRAM_MODELS)
COMPILE_ARGS    += -DFUNCTIONAL

ifneq ($(GATES),yes)

# RTL simulation:
SIM_BUILD				= sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))

else

# Gate level simulation:
SIM_BUILD				= sim_build/gl
COMPILE_ARGS    += -DGL_TEST
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
COCOTB_TEST_MODULES = test_flowtest

# include cocotb's make rules to take care of the simulator setup
include $(shell cocotb-config --makefiles)/Makefile.sim
