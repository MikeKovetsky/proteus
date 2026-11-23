# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Tests of the whole chip through Tiny Tapeout's 24 pins, as the demo board's RP2350 would drive it.

Every test loads a compiled program through the nibble loader (port 2), talks to the machines through the
nibble host link, and judges the protocol pins with its own decoder, not the golden model's. They use only
the top-level ports, so they also run on the gate-level netlist (GATES=yes). The one exception is
check_sram_ties, which looks inside both SRAM macros and finds them in either netlist.

The 14 bake-off scenarios, protocol by protocol and in absolute time, are in test_scenarios.py.

The cycle-by-cycle comparison with the golden model is the lockstep suite in test/lockstep/ (make lockstep).
"""

import cocotb
from cocotb.handle import HierarchyObject
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge

from host_link import (PORT_CH0_CH1, PORT_CH3_CH2, PORT_LOADER, PORT_NONE, ST_HAS, ST_OVERFLOW, ST_ROOM,
                       ST_RUNNING, Host, program, reset, start_clock)
from bakeoff.cand_table.compiler import compile_text  # host_link puts the repository on the path

UART_BIT = 521  # clocks per bit in uart_tx.fsm and uart_rx.fsm (60 MHz / 115200)
UART3M_BIT = 20  # clocks per bit in uart_3m.fsm

# Every byte the host writes to channel 3 comes back on channel 2. It exercises port 1 of the host link.
ECHO = """
machine echo
idle:   wait rx3
        S=pop3
        push2=S => idle
"""

SCK, MOSI, MISO, CS = 0, 1, 2, 3  # the pins of spi_target_m0.fsm
SPI_HALF = 12  # clocks per half period of SCK (2.5 MHz), slow next to the 3-clock pads


async def start(dut):
    """Clock at 60 MHz, pins idle high, reset for 10 clocks, SRAM ties checked. Returns the host. These tests
    count clocks, not nanoseconds."""
    start_clock(dut)
    host = Host(dut)
    dut.ena.value = 1
    dut.uio_in.value = 0xFF
    await reset(dut, host)
    check_sram_ties(dut)
    return host


SRAM = "RM_IHPSG13_1P_512x32_c2_bm_bist"  # the macro the table is built from (src/proteus_table.v)


def table_srams(dut):
    """The table's two SRAM macros, word A's first. The RTL keeps the hierarchy: core.table_ram.ram_a and
    ram_b. The gate-level netlist is flat, and cocotb cannot look up its escaped instance names
    (\\core.table_ram.ram_a) by name, so there the macros are found among the top's instances by module."""
    top = dut.user_project
    rams = sorted((h for h in top if isinstance(h, HierarchyObject) and h._def_name == SRAM), key=lambda h: h._name)
    if not rams:
        rams = [top.core.table_ram.ram_a, top.core.table_ram.ram_b]
    assert len(rams) == 2, "found %d %s macros, expected 2" % (len(rams), SRAM)
    return rams


def check_sram_ties(dut):
    """A_DLY must be tied high on both macros. The PDK's SRAM model checks it, but it only prints an error and
    calls $stop, which Icarus runs past when nothing answers on its stdin, as in CI. So the test checks it, on
    RTL and on the gate-level netlist. The pin is found by iterating the macro: the netlist's names do not
    resolve."""
    for ram in table_srams(dut):
        dly = next((pin for pin in ram if pin._name == "A_DLY"), None)
        assert dly is not None, "%s has no A_DLY pin" % ram._name
        assert str(dly.value) == "1", "%s.A_DLY is %s; the SRAM needs it tied to 1" % (ram._name, dly.value)


class Pads:
    """The far end of the protocol pins: what it drives on uio_in, every pin high to start with. It keeps its
    own copy of the levels: a write to a cocotb handle lands at the end of the time step, so reading uio_in
    back would undo a second change made in the same step."""

    def __init__(self, dut):
        self.dut, self.levels = dut, 0xFF
        dut.uio_in.value = self.levels

    def set(self, pin, level):
        self.levels = (self.levels | 1 << pin) if level else (self.levels & ~(1 << pin))
        self.dut.uio_in.value = self.levels


def pin_level(dut, pin):
    """Protocol pin `pin` as the far end sees it: the chip's level when it drives the pin, else the pull-up's 1."""
    if int(dut.uio_oe.value) >> pin & 1:
        return int(dut.uio_out.value) >> pin & 1
    return 1


class Line:
    """Samples one protocol pin every clock, as the far end sees it (`pin_level`)."""

    def __init__(self, dut, pin):
        self.dut, self.pin, self.levels = dut, pin, []
        self.task = cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await FallingEdge(self.dut.clk)
            self.levels.append(pin_level(self.dut, self.pin))

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


async def drive_uart(pads, pin, data, bit):
    for byte in data:
        for level in [0] + [(byte >> n) & 1 for n in range(8)] + [1]:
            pads.set(pin, level)
            await ClockCycles(pads.dut.clk, bit)


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
    pads = Pads(dut)
    await host.load(program("uart_rx"))
    want = bytes([0x3C, 0x00, 0xFF])
    await drive_uart(pads, 1, want, UART_BIT)
    got = bytes([await host.read(PORT_CH0_CH1) for _ in want])
    assert got == want, "sent %s on P1, the host read %s" % (want.hex(), got.hex())
    assert int(dut.uio_oe.value) == 0, "the receiver drove a pin"


@cocotb.test()
async def test_uart_3m_two_machines(dut):
    """uart_3m: two machines share the table, one sending on P0 and one receiving on P1, both at once."""
    host = await start(dut)
    pads = Pads(dut)
    await host.load(program("uart_3m"))
    to_send, to_receive = bytes([0x12, 0xEF, 0x80, 0x7E]), bytes([0xA5, 0x01, 0xC3, 0x5A])
    line = Line(dut, 0)
    receiver = cocotb.start_soon(drive_uart(pads, 1, to_receive, UART3M_BIT))
    await host.send(PORT_CH0_CH1, to_send)
    await receiver
    got = bytes([await host.read(PORT_CH0_CH1) for _ in to_receive])
    await ClockCycles(dut.clk, 10 * UART3M_BIT)
    sent = decode_uart(line.stop(), UART3M_BIT)
    assert sent == to_send, "TX: host gave %s, P0 sent %s" % (to_send.hex(), sent.hex())
    assert got == to_receive, "RX: P1 carried %s, the host read %s" % (to_receive.hex(), got.hex())


@cocotb.test()
async def test_spi_target_and_pad_timing(dut):
    """spi_target_m0: one mode-0 byte each way through the pins. Then the pads, which the lockstep suite does
    not see (it drives the core directly): when CS rises, `MISO=z when CS` releases MISO on the third rising
    edge, two synchroniser flops in and the pad register out. That is the bake-off harness's pad model, and
    bakeoff/cand_table/selfcheck.py measures the same 3 clocks on the golden model."""
    host = await start(dut)
    pads = Pads(dut)  # CS starts high
    pads.set(SCK, 0)
    pads.set(MOSI, 0)
    await host.load(program("spi_target_m0"))
    await host.send(PORT_CH0_CH1, bytes([0xA5]))  # what the target answers with
    await ClockCycles(dut.clk, 20)
    pads.set(CS, 0)
    await ClockCycles(dut.clk, SPI_HALF)
    assert int(dut.uio_oe.value) >> MISO & 1, "MISO is not driven while CS is low"

    miso = 0
    for n in range(7, -1, -1):  # MSB first; MOSI changes while SCK is low, both ends sample as it rises
        pads.set(MOSI, 0x3C >> n & 1)
        await ClockCycles(dut.clk, SPI_HALF)
        pads.set(SCK, 1)
        miso = miso << 1 | pin_level(dut, MISO)
        await ClockCycles(dut.clk, SPI_HALF)
        pads.set(SCK, 0)
    await ClockCycles(dut.clk, SPI_HALF)
    assert miso == 0xA5, "the target answered %02x on MISO, expected a5" % miso
    assert int(dut.uio_oe.value) >> MISO & 1, "MISO was released while CS is low"

    await FallingEdge(dut.clk)
    pads.set(CS, 1)
    released = None
    for edge in range(1, 7):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        if not int(dut.uio_oe.value) >> MISO & 1:
            released = edge
            break
    assert released == 3, (
        "MISO was released on rising edge %s after CS rose (None: not by the 6th); the pads give 3" % released)
    got = await host.read(PORT_CH0_CH1)
    assert got == 0x3C, "the controller sent 3c on MOSI, the host read %02x" % got


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
async def test_host_writes_to_full_channels(dut):
    """ROOM falls when the selected write channel holds 4 bytes, and only for that channel. A byte the host
    writes anyway is dropped and sets OVERFLOW, on channel 0 (port 0) and on channel 3 (port 1). Port 3 takes
    nothing: a byte written there before the load would break the image if it reached the loader."""
    host = await start(dut)
    await host.write(PORT_NONE, 0x01)
    assert host.status() & (ST_ROOM | ST_HAS | ST_OVERFLOW) == 0, "port 3 shows %02x" % host.status()
    for port, other in ((PORT_CH0_CH1, PORT_CH3_CH2), (PORT_CH3_CH2, PORT_CH0_CH1)):
        await host.load(program("uart_rx"))  # pops neither channel 0 nor channel 3
        await host.send(port, bytes([0x40, 0x41, 0x42, 0x43]))  # waits for ROOM before each byte
        assert host.status() & ST_ROOM == 0, "port %d: ROOM is high with 4 bytes in its channel" % port
        assert host.status() & ST_OVERFLOW == 0, "port %d: OVERFLOW before a byte was dropped" % port
        await host.select(other)
        assert host.status() & ST_ROOM, "port %d: ROOM is low, but only port %d's channel is full" % (other, port)
        await host.write(port, 0x44)  # a fifth byte, without ROOM
        assert host.status() & ST_OVERFLOW, "port %d: a byte written to a full channel did not set OVERFLOW" % port
        await reset(dut, host)  # clears OVERFLOW (reset checks it) for the second channel


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
