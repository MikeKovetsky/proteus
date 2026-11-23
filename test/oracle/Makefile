# Independent third-party protocol oracle tests (cocotbext-uart, cocotbext-i2c, cocotbext-spi).
SIM ?= icarus
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../../src
PROJECT_SOURCES = project.v proteus_core.v proteus_fifo.v proteus_table.v proteus_trace.v
SRAM_MODELS = $(PWD)/../ihp_sram/RM_IHPSG13_1P_512x32_c2_bm_bist.v $(PWD)/../ihp_sram/RM_IHPSG13_1P_core_behavioral_bm_bist.v

SIM_BUILD = sim_build
VERILOG_SOURCES += $(SRAM_MODELS)
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))
VERILOG_SOURCES += $(PWD)/tb_oracle.v
COMPILE_ARGS += -DFUNCTIONAL -I$(SRC_DIR)

TOPLEVEL = tb_oracle
COCOTB_TEST_MODULES = test_oracle
PYTHONPATH := $(PWD)/..:$(PYTHONPATH)
export PYTHONPATH

include $(shell cocotb-config --makefiles)/Makefile.sim
