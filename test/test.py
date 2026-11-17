# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Tests of the whole chip through Tiny Tapeout's 24 pins, as the demo board's RP2350 would drive it.

Every test loads a compiled program through the nibble loader (port 2), talks to the machines through the
nibble host link, and judges the protocol pins with its own decoder, not the golden model's. They use only
the top-level ports, so they also run on the gate-level netlist (GATES=yes).

The cycle-by-cycle comparison with the golden model is the lockstep suite in test/lockstep/ (make lockstep).
"""

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bakeoff.cand_table.compiler import compile_file, compile_text  # noqa: E402

PROGRAMS = ROOT / "bakeoff" / "cand_table" / "programs"
UART_BIT = 521  # clocks per bit in uart_tx.fsm and uart_rx.fsm (60 MHz / 115200)
UART3M_BIT = 20  # clocks per bit in uart_3m.fsm

# Every byte the host writes to channel 3 comes back on channel 2. It exercises port 1 of the host link.
ECHO = """
machine echo
idle:   wait rx3
        S=pop3
        push2=S => idle
"""

ST_ROOM, ST_HAS, ST_RUNNING, ST_OVERFLOW = 0x10, 0x20, 0x40, 0x80
PORT_CH0_CH1, PORT_CH3_CH2, PORT_LOADER = 0, 1, 2


def program(name):
    return compile_file(PROGRAMS / (name + ".fsm")).image


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


async def start(dut):
    """Clock at 60 MHz (16 ns here), pins idle high, reset for 10 clocks. Returns the host."""
    cocotb.start_soon(Clock(dut.clk, 16, unit="ns").start())
    host = Host(dut)
    dut.ena.value = 1
    dut.uio_in.value = 0xFF
    await reset(dut, host)
    return host


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


class Line:
    """Samples one protocol pin every clock, as the far end sees it: the chip's level when it drives the
    pin, otherwise the pull-up's 1."""

    def __init__(self, dut, pin):
        self.dut, self.mask, self.levels = dut, 1 << pin, []
        self.task = cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await FallingEdge(self.dut.clk)
            oe = int(self.dut.uio_oe.value)
            self.levels.append(1 if not oe & self.mask or int(self.dut.uio_out.value) & self.mask else 0)

    def stop(self):
        self.task.cancel()
        return self.levels


def decode_uart(levels, bit):
    """8N1 frames, LSB first. Every edge inside a frame must fall within one clock of the bit grid that
    starts at the frame's start edge, and the stop bit must be high."""
    data, i = [], 1
    while i < len(levels):
        if not (levels[i - 1] and not levels[i]):
            i += 1
            continue
        start = i
        assert start + 10 * bit <= len(levels), "a frame at clock %d runs past the capture" % start
        bits = [levels[start + int((k + 0.5) * bit)] for k in range(10)]
        assert bits[0] == 0 and bits[9] == 1, "bad frame at clock %d: %s" % (start, bits)
        for j in range(start + 1, start + 9 * bit + bit // 2):
            if levels[j] != levels[j - 1]:
                offset = (j - start) % bit
                assert min(offset, bit - offset) <= 1, "edge at clock %d is off the bit grid" % j
        data.append(sum(b << n for n, b in enumerate(bits[1:9])))
        i = start + 9 * bit + bit // 2
    return bytes(data)


async def drive_uart(dut, pin, data, bit):
    for byte in data:
        for level in [0] + [(byte >> n) & 1 for n in range(8)] + [1]:
            current = int(dut.uio_in.value)
            dut.uio_in.value = (current | 1 << pin) if level else (current & ~(1 << pin))
            await ClockCycles(dut.clk, bit)


async def uart_tx_check(dut, host, data, bit):
    line = Line(dut, 0)
    await host.send(PORT_CH0_CH1, data)
    await ClockCycles(dut.clk, (len(data) + 1) * 10 * bit)
    got = decode_uart(line.stop(), bit)
    assert got == data, "sent %s, decoded %s on P0" % (data.hex(), got.hex())


@cocotb.test()
async def test_uart_tx(dut):
    """uart_tx: the host writes 3 bytes to channel 0; P0 sends them as 115200-baud 8N1 frames."""
    host = await start(dut)
    assert host.status() & ST_RUNNING == 0, "running before a program was loaded"
    await host.load(program("uart_tx"))
    await uart_tx_check(dut, host, bytes([0x55, 0xA3, 0x0F]), UART_BIT)
    assert host.status() & ST_OVERFLOW == 0


@cocotb.test()
async def test_uart_rx(dut):
    """uart_rx: 3 frames at 115200 baud on P1 come back to the host on channel 1."""
    host = await start(dut)
    await host.load(program("uart_rx"))
    want = bytes([0x3C, 0x00, 0xFF])
    await drive_uart(dut, 1, want, UART_BIT)
    got = bytes([await host.read(PORT_CH0_CH1) for _ in want])
    assert got == want, "sent %s on P1, the host read %s" % (want.hex(), got.hex())
    assert int(dut.uio_oe.value) == 0, "the receiver drove a pin"


@cocotb.test()
async def test_uart_3m_two_machines(dut):
    """uart_3m: two machines share the table, one sending on P0 and one receiving on P1, both at once."""
    host = await start(dut)
    await host.load(program("uart_3m"))
    to_send, to_receive = bytes([0x12, 0xEF, 0x80, 0x7E]), bytes([0xA5, 0x01, 0xC3, 0x5A])
    line = Line(dut, 0)
    receiver = cocotb.start_soon(drive_uart(dut, 1, to_receive, UART3M_BIT))
    await host.send(PORT_CH0_CH1, to_send)
    await receiver
    got = bytes([await host.read(PORT_CH0_CH1) for _ in to_receive])
    await ClockCycles(dut.clk, 10 * UART3M_BIT)
    sent = decode_uart(line.stop(), UART3M_BIT)
    assert sent == to_send, "TX: host gave %s, P0 sent %s" % (to_send.hex(), sent.hex())
    assert got == to_receive, "RX: P1 carried %s, the host read %s" % (to_receive.hex(), got.hex())


@cocotb.test()
async def test_port1_echo_and_overflow(dut):
    """Port 1 writes channel 3 and reads channel 2. A push into a full channel 2 sets the overflow bit."""
    host = await start(dut)
    await host.load(compile_text(ECHO).image)
    for byte in (0x00, 0x5A, 0xFF):
        await host.write(PORT_CH3_CH2, byte)
        got = await host.read(PORT_CH3_CH2)
        assert got == byte, "echo of %02x came back as %02x" % (byte, got)
    assert host.status() & ST_OVERFLOW == 0
    burst = bytes(range(0x30, 0x35))  # five bytes into a 4-byte channel 2 that nobody reads
    await host.send(PORT_CH3_CH2, burst)
    await ClockCycles(dut.clk, 20)
    assert host.status() & ST_OVERFLOW, "five pushes into a 4-byte FIFO did not set overflow"
    got = bytes([await host.read(PORT_CH3_CH2) for _ in range(4)])
    assert got == burst[:4], "after the overflow the host read %s" % got.hex()
    assert host.status() & ST_HAS == 0, "a fifth byte was kept"


@cocotb.test()
async def test_reset_reload_run_again(dut):
    """R5: reset in the middle of a frame clears the pins and stops the machines; a reload runs again,
    the same program or another. An image sent without a reset is refused and flagged."""
    host = await start(dut)
    await host.load(program("uart_3m"))
    await uart_tx_check(dut, host, bytes([0x96, 0x69]), UART3M_BIT)

    await host.send(PORT_CH0_CH1, bytes([0x00]))
    await ClockCycles(dut.clk, 3 * UART3M_BIT)  # inside the frame
    assert int(dut.uio_oe.value) & 1, "P0 is not driven while sending"
    await reset(dut, host)
    assert host.status() & ST_RUNNING == 0 and int(dut.uio_oe.value) == 0, "not idle after reset"
    await ClockCycles(dut.clk, 50)
    assert host.status() & ST_RUNNING == 0 and int(dut.uio_oe.value) == 0, "ran with no program"

    await host.load(program("uart_3m"))
    await uart_tx_check(dut, host, bytes([0x3D, 0xC2]), UART3M_BIT)

    await reset(dut, host)
    await host.load(compile_text(ECHO).image)
    await host.write(PORT_CH3_CH2, 0x77)
    assert await host.read(PORT_CH3_CH2) == 0x77, "the echo program did not run after the reload"

    await host.write(PORT_LOADER, 0x01)  # the start of another image, without a reset
    await ClockCycles(dut.clk, 5)
    assert host.status() & ST_OVERFLOW, "a loader byte after the load was not flagged"
    await host.write(PORT_CH3_CH2, 0x78)
    assert await host.read(PORT_CH3_CH2) == 0x78, "the running program stopped"
