# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The host's side of the pins, shared by every pin test: the clock, reset, the nibble link and the loader."""

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bakeoff.cand_table.compiler import compile_file  # noqa: E402

PROGRAMS = ROOT / "bakeoff" / "cand_table" / "programs"
# 60 MHz: 16.6667 ns, rounded to an even count of the simulator's picoseconds (60.0024 MHz). bench.py measures
# time on the chip's clock, so the rounding reaches no limit.
CLOCK_PS = 16666

ST_ROOM, ST_HAS, ST_RUNNING, ST_OVERFLOW = 0x10, 0x20, 0x40, 0x80
PORT_CH0_CH1, PORT_CH3_CH2, PORT_LOADER, PORT_NONE = 0, 1, 2, 3


def program(name):
    return compile_file(PROGRAMS / (name + ".fsm")).image


def start_clock(dut):
    cocotb.start_soon(Clock(dut.clk, CLOCK_PS, unit="ps").start())


class Host:
    """The host's side of the pins: ui_in[3:0] nibble, ui_in[5:4] port, ui_in[6] write and ui_in[7] read
    strobes (every edge moves one nibble), uo_out[3:0] nibble out and uo_out[7:4] status."""

    def __init__(self, dut):
        self.dut = dut
        self.nib = 0
        self.port = 0
        self.wr = 0
        self.rd = 0

    def drive(self):
        self.dut.ui_in.value = self.nib | self.port << 4 | self.wr << 6 | self.rd << 7

    def status(self):
        return int(self.dut.uo_out.value)

    async def clocks(self, n):
        await ClockCycles(self.dut.clk, n)

    async def select(self, port):
        if port != self.port:
            self.port = port
            self.drive()
            await self.clocks(4)  # the port goes through the synchroniser too

    async def wait_for(self, mask, what, limit):
        for _ in range(limit):
            if self.status() & mask:
                return
            await self.clocks(1)
        raise AssertionError("no %s within %d clocks (uo_out = %02x)" % (what, limit, self.status()))

    async def write(self, port, byte):
        """One byte, low nibble first. The nibble is set a clock before its strobe edge and held 4 after."""
        await self.select(port)
        for nib in (byte & 15, byte >> 4):
            self.nib = nib
            self.drive()
            await self.clocks(1)
            self.wr ^= 1
            self.drive()
            await self.clocks(4)

    async def send(self, port, data, limit=20000):
        for byte in data:
            await self.select(port)
            await self.wait_for(ST_ROOM, "room on port %d" % port, limit)
            await self.write(port, byte)

    async def read(self, port, limit=20000):
        """One byte from the port's read channel: low nibble, a strobe edge, high nibble, an edge that pops."""
        await self.select(port)
        await self.wait_for(ST_HAS, "byte on port %d" % port, limit)
        low = self.status() & 15
        self.rd ^= 1
        self.drive()
        await self.clocks(4)
        high = self.status() & 15
        self.rd ^= 1
        self.drive()
        await self.clocks(4)
        return low | high << 4

    async def load(self, image):
        await self.send(PORT_LOADER, image)
        await self.wait_for(ST_RUNNING, "running after the load", 1000)


async def reset(dut, host):
    """The strobes are low during reset, so the first edge after it is a real one."""
    host.nib = host.port = host.wr = host.rd = 0
    host.drive()
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    assert int(dut.uio_oe.value) == 0, "uio_oe is %02x in reset" % int(dut.uio_oe.value)
    assert host.status() & (ST_RUNNING | ST_OVERFLOW) == 0, "uo_out is %02x in reset" % host.status()
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
