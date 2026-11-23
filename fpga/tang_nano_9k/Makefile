# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
#
# Open-source Gowin bitstream build for Sipeed Tang Nano 9K (GW1NR-LV9QN88PC6/I5)
# Requires ~/oss-cad-suite/environment (yosys, nextpnr-himbaechel, gowin_pack, openFPGALoader).

DEVICE  := GW1NR-LV9QN88PC6/I5
FAMILY  := GW1N-9C
TOP     := tang_nano_9k_top
BUILD   := build
# Direct 27 MHz crystal closes timing cleanly on GW1NR-9C C6/I5 (Fmax ~30.7 MHz).
# Pass USE_PLL=1 FREQ=60 to build with the 27->60 MHz rPLL (for timing experiments).
USE_PLL ?= 0
FREQ    ?= 27

ifeq ($(USE_PLL),0)
YOSYS_DEFINES := -DFPGA_CLK_27M
else
YOSYS_DEFINES :=
endif

SRC_DIR := ../../src
RTL_SRCS := \
	$(SRC_DIR)/project.v \
	$(SRC_DIR)/proteus_core.v \
	$(SRC_DIR)/proteus_fifo.v \
	$(SRC_DIR)/proteus_table.v \
	$(SRC_DIR)/proteus_trace.v \
	sram_gowin.v \
	proteus_pll.v \
	tang_nano_9k_top.v

SYNTH_JSON := $(BUILD)/proteus_tang9k.json
PNR_JSON   := $(BUILD)/proteus_tang9k_pnr.json
BITSTREAM  := $(BUILD)/proteus_tang9k.fs

.PHONY: all bitstream flash sram lint clean

all: bitstream

$(BUILD):
	mkdir -p $(BUILD)

$(SYNTH_JSON): $(RTL_SRCS) | $(BUILD)
	yosys -p "read_verilog $(YOSYS_DEFINES) $(RTL_SRCS); synth_gowin -top $(TOP) -family gw1n -json $@" \
		-l $(BUILD)/yosys.log

$(PNR_JSON): $(SYNTH_JSON) tang_nano_9k.cst
	nextpnr-himbaechel --json $(SYNTH_JSON) \
		--write $@ \
		--device $(DEVICE) \
		--vopt family=$(FAMILY) \
		--vopt cst=tang_nano_9k.cst \
		--freq $(FREQ) \
		--report $(BUILD)/pnr_report.json \
		-l $(BUILD)/nextpnr.log

$(BITSTREAM): $(PNR_JSON)
	gowin_pack -d $(FAMILY) -o $@ $(PNR_JSON)

bitstream: $(BITSTREAM)
	@echo "Built Tang Nano 9K bitstream: $(BITSTREAM)"

# Load volatile SRAM (instant, no flash wear — great for testing)
sram: $(BITSTREAM)
	openFPGALoader -b tangnano9k $(BITSTREAM)

# Write persistent SPI flash (boots Proteus automatically on USB power)
flash: $(BITSTREAM)
	openFPGALoader -b tangnano9k -f $(BITSTREAM)

lint:
	verilator --lint-only -Wall -DSIM --top-module $(TOP) $(RTL_SRCS)

clean:
	rm -rf $(BUILD)
