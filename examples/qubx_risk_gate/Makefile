# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
#
# Build & test the Proteus Inline Pre-Trade Hardware Risk Gate + Qubx HIL Demo.

SRC_DIR  := ../../src
SRAM_DIR := ../../test/ihp_sram
BUILD    := build
SIM_BIN  := $(BUILD)/sim_risk_gate

RTL_SRCS := \
	$(SRC_DIR)/project.v \
	$(SRC_DIR)/proteus_core.v \
	$(SRC_DIR)/proteus_fifo.v \
	$(SRC_DIR)/proteus_table.v \
	$(SRC_DIR)/proteus_trace.v \
	$(SRAM_DIR)/RM_IHPSG13_1P_512x32_c2_bm_bist.v \
	$(SRAM_DIR)/RM_IHPSG13_1P_core_behavioral_bm_bist.v

.PHONY: all sim test demo clean

all: sim test

$(SIM_BIN): $(RTL_SRCS) sim_risk_gate.cpp
	mkdir -p $(BUILD)
	verilator --cc --exe --build -j 0 --no-timing \
		--Mdir $(BUILD)/obj_dir \
		--top-module tt_um_mikekovetsky_proteus \
		-Wno-TIMESCALEMOD -Wno-UNUSEDSIGNAL -Wno-UNDRIVEN -Wno-PINMISSING -Wno-WIDTH -Wno-SPECIFYIGN -Wno-STMTDLY \
		-o ../sim_risk_gate \
		$(RTL_SRCS) sim_risk_gate.cpp

sim: $(SIM_BIN)

test: $(SIM_BIN)
	python3 test_risk_gate.py

demo: $(SIM_BIN)
	python3 demo_tui.py

clean:
	rm -rf $(BUILD)
