# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

# The placeholder design adds ui_in and uio_in, modulo 256. These pairs drive every output bit
# low and high, and run a carry through every bit and out of the top one.
DIRECTED = [
    (20, 30),  # 50, the template's example
    (0x00, 0x00),  # every output bit low
    (0xAA, 0x55),  # every output bit high, with no carry
    (0x01, 0xFF),  # a carry through all eight bits, which wraps to 0
    (0x80, 0x80),  # only the carry out of the top bit, which is dropped
    (0xFF, 0xFF),  # 0xFE
]
RANDOM_PAIRS = 100  # cocotb logs the random seed, so a failure can be replayed


@cocotb.test()
async def test_project(dut):
    dut._log.info("Start")

    # Set the clock period to 10 us (100 KHz)
    clock = Clock(dut.clk, 10, unit="us")
    cocotb.start_soon(clock.start())

    # Reset
    dut._log.info("Reset")
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1

    dut._log.info("Test project behavior")

    pairs = DIRECTED + [
        (random.randrange(256), random.randrange(256)) for _ in range(RANDOM_PAIRS)
    ]
    for a, b in pairs:
        dut.ui_in.value = a
        dut.uio_in.value = b

        # Wait for one clock cycle to see the output values
        await ClockCycles(dut.clk, 1)

        expected = (a + b) % 256
        assert (
            dut.uo_out.value == expected
        ), f"{a} + {b}: uo_out is {dut.uo_out.value}, expected {expected:08b}"
        # The bidirectional pins carry B in, so the design must never drive them.
        assert dut.uio_oe.value == 0, f"uio_oe is {dut.uio_oe.value}, expected 0"
        assert dut.uio_out.value == 0, f"uio_out is {dut.uio_out.value}, expected 0"
