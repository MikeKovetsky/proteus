# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Pin-level tests for RTL v2 features through Tiny Tapeout's 24 pins (works on RTL and gate-level netlist):

  1. 16-rule masked Byte-Class TCAM + 1-clock indexed dispatch (tcam4/8/16, S[7:4], S[hi:lo])
  2. 4th input pin (in_d) + 5 MHz SPI mode-0 target with 1-clock TCAM opcode dispatch and nRESET
  3. Dual-edge 8.33 ns half-clock input sampling (in_b_half)
  4. 10BASE-T Ethernet receive at +/-13.5 ns IEEE 802.3 jitter (closes gap_eth10_rx_jitter)
  5. Port-3 32-entry hardware pin-trace buffer (mask, arm-on-trigger, exact cycle deltas, heartbeat)
  6. Host-read strobe guard (stray rd_strobe edges with HAS_BYTE == 0 never desync or drop bytes)
"""

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge

from host_link import (PORT_CH0_CH1, PORT_CH3_CH2, PORT_NONE, ST_HAS, ST_OVERFLOW, ST_ROOM,
                       Host, program, reset, start_clock)
from bakeoff.cand_table.compiler import compile_text  # host_link puts the repository on the path
from bench import boot, now
from protocols import crc, eth10, i2c, usb_ls
from protocols.wave import CLK, EPS, US
from test import Pads, pin_level, start

PORT_TRACE = PORT_NONE  # Port 3 is the hardware pin-trace buffer once RUNNING == 1

TCAM_DISPATCH_PROG = """
# Priority check: rule 0 (exact 0x85 -> class 3) precedes rule 1 (range 0x80..0x8F -> class 2).
tcam 0x85/0xFF -> 3
tcam 0x80/0xF0 -> 2
tcam 0x03/0xFF -> 1

machine disp
idle:   wait rx0
        S=pop0
        dispatch tcam4 => miss | 1: c1 | 2: c2 | 3: c3
miss:   dispatch S[7:4] => other | 0xA: nib_a
nib_a:  dispatch S[1:0] => other | 2: slice_2
c1:     push1=0x11 => idle
c2:     push1=0x22 => idle
c3:     push1=0x33 => idle
slice_2: push1=0xAA => idle
other:  push1=0x00 => idle
"""

# 4-input SPI mode-0 target with 1-clock TCAM opcode dispatch AND hardware nRESET on P4!
# Commands: 0x03 -> next byte answers 0x5A; 0x80..0x8F (masked range) -> next byte answers 0xC3;
# other -> next byte answers 0x00. Pulsing nRESET low returns immediately to idle (0x00).
SPI_5MHZ_TCAM_NRESET = """
tcam 0x03/0xFF -> 1
tcam 0x80/0xF0 -> 2

machine spi_tcam
    out MISO=P2
    in  SCK=P0, MOSI=P1, CS=P3, NRST=P4
    order msb

rst:    MISO=z, R=0x00
wait_r: wait NRST
idle:   MISO=z, wait => !NRST: rst | !CS: start
start:  S=R, K=7
        MISO=d, wait => CS: idle | SCK: b0
b0:     shift MOSI, K-=1 => !NRST: rst | low
put:    MISO=d, wait => CS: idle | SCK: take
take:   shift MOSI, K-=1 => kz: last | low
low:    wait => CS: idle | !SCK: put
last:   # 8th bit just shifted into S! 1-clock TCAM dispatch decides next answer R immediately!
        dispatch tcam4 => d_def | 1: d_5a | 2: d_c3
d_5a:   R=0x5A => next_b
d_c3:   R=0xC3 => next_b
d_def:  R=0x00 => next_b
next_b: wait => CS: idle | !SCK: start
"""

ECHO = """
machine echo
idle:   wait rx3
        S=pop3
        push2=S => idle
"""


@cocotb.test()
async def test_v2_tcam_and_indexed_dispatch(dut):
    """v2: 16-rule masked TCAM priority + 1-clock tcam4, S[7:4] nibble and S[1:0] slice dispatch."""
    host = await start(dut)
    await host.load(compile_text(TCAM_DISPATCH_PROG).image)
    cases = [
        (0x03, 0x11),  # exact TCAM rule 2 -> class 1
        (0x80, 0x22),  # range TCAM rule 1 -> class 2
        (0x8F, 0x22),  # range TCAM rule 1 -> class 2
        (0x85, 0x33),  # priority TCAM rule 0 beats rule 1 -> class 3
        (0xA2, 0xAA),  # TCAM miss -> S[7:4]==0xA -> S[1:0]==2
        (0xAE, 0xAA),  # TCAM miss -> S[7:4]==0xA -> S[1:0]==2
        (0xA1, 0x00),  # TCAM miss -> S[7:4]==0xA -> S[1:0]==1 -> other
        (0x55, 0x00),  # TCAM miss -> other
    ]
    for byte, want in cases:
        await host.write(PORT_CH0_CH1, byte)
        got = await host.read(PORT_CH0_CH1)
        assert got == want, "dispatch(0x%02x) gave 0x%02x, expected 0x%02x" % (byte, got, want)
    assert host.status() & ST_OVERFLOW == 0


@cocotb.test()
async def test_v2_spi_5mhz_tcam_with_nreset(dut):
    """v2: 5 MHz SPI mode-0 target using 1-clock TCAM dispatch and 4th input nRESET (P4)."""
    host = await start(dut)
    pads = Pads(dut)
    SCK, MOSI, MISO, CS, NRST = 0, 1, 2, 3, 4
    pads.set(SCK, 0)
    pads.set(MOSI, 0)
    pads.set(CS, 1)
    pads.set(NRST, 1)
    await host.load(compile_text(SPI_5MHZ_TCAM_NRESET).image)
    await ClockCycles(dut.clk, 10)

    async def xfer(tx_bytes):
        # 5 MHz SPI at 60 MHz chip clock = 12 clocks/bit = 6 clocks per half-period!
        pads.set(CS, 0)
        await ClockCycles(dut.clk, 6)
        rx = []
        for b in tx_bytes:
            got = 0
            for bit in range(7, -1, -1):
                pads.set(MOSI, (b >> bit) & 1)
                await ClockCycles(dut.clk, 6)
                pads.set(SCK, 1)
                got = (got << 1) | pin_level(dut, MISO)
                await ClockCycles(dut.clk, 6)
                pads.set(SCK, 0)
            rx.append(got)
        await ClockCycles(dut.clk, 6)
        pads.set(CS, 1)
        await ClockCycles(dut.clk, 8)
        return bytes(rx)

    # Transaction 1: send 0x03 -> next byte answers 0x5A; then 0x8C (masked range) -> next answers 0xC3!
    got = await xfer(bytes([0x03, 0x8C, 0x00]))
    assert got == bytes([0x00, 0x5A, 0xC3]), "5 MHz SPI TCAM gave %s, expected 005ac3" % got.hex()

    # Now pulse nRESET (4th input P4) while R is still 0x00? First arm R=0x5A, then pulse nRESET!
    got2 = await xfer(bytes([0x03]))
    assert got2 == bytes([0x00])
    # Without nRESET, next transaction would start with 0x5A. Pulse nRESET low to clear R back to 0x00!
    pads.set(NRST, 0)
    await ClockCycles(dut.clk, 6)
    pads.set(NRST, 1)
    await ClockCycles(dut.clk, 6)
    got3 = await xfer(bytes([0x00]))
    assert got3 == bytes([0x00]), "after nRESET on 4th input P4, got %s instead of 00" % got3.hex()


@cocotb.test()
async def test_v2_eth10_rx_13_5ns_jitter(dut):
    """v2: 10BASE-T Ethernet receive passes full +/-13.5 ns IEEE 802.3 jitter using P2.half (8.33 ns)."""
    chip = await boot(dut, "eth10_rx", pulled_low=[2])
    frame = crc.with_crc(crc.CRC32_ETHERNET, bytes(range(60)))

    def jitter(n):
        return eth10.RECEIVE_JITTER * (1 if n // 2 % 2 else -1)

    events, _ = eth10.send(2, frame, now() + 1 * US, ppm=2 * eth10.PPM, jitter=jitter)
    await chip.board.play(events)
    await chip.expect(ch1=frame, ch2=bytes([0, 64, 0]), within=20 * US)


@cocotb.test()
async def test_v2_port3_pin_trace_buffer(dut):
    """v2: Port 3 hardware pin-trace buffer captures real pin edges and exact cycle deltas + heartbeat."""
    host = await start(dut)
    await host.load(program("uart_tx"))  # P0 idles high (1) and sends 115200-baud (521-clock) frames
    await ClockCycles(dut.clk, 20)

    # Port 3 is active once RUNNING == 1
    await host.select(PORT_TRACE)
    assert host.status() & ST_ROOM, "Port 3 ROOM should be high when running"
    assert host.status() & ST_HAS == 0, "Port 3 trace should be empty before arming"

    # Mask to P0 only (cmd 0x03, mask 0x01), then arm on trigger (cmd 0x02)
    await host.write(PORT_TRACE, 0x03)
    await host.write(PORT_TRACE, 0x01)
    await host.write(PORT_TRACE, 0x02)
    await ClockCycles(dut.clk, 20)
    assert host.status() & ST_HAS == 0, "armed trace should stay empty before P0 toggles"

    # Send 0x55 on UART (start=0, bits=1,0,1,0,1,0,1,0, stop=1 -> 10 clean transitions, 521 clocks apart!)
    await host.write(PORT_CH0_CH1, 0x55)
    await ClockCycles(dut.clk, 2500)  # enough for start bit + 3 data bits (~4 transitions + heartbeats)

    # Stop trace (cmd 0x00) and read captured {pins, delta} events from Port 3
    await host.write(PORT_TRACE, 0x00)
    events = []
    await host.select(PORT_TRACE)
    while host.status() & ST_HAS:
        pins_b = await host.read(PORT_TRACE)
        delta_b = await host.read(PORT_TRACE)
        events.append((pins_b & 1, delta_b))

    assert len(events) >= 6, "expected >= 6 trace events (edges + heartbeats), got %d: %s" % (len(events), events)
    # First event is the armed trigger (start bit falling to 0, delta == 0)
    assert events[0] == (0, 0), "first armed event should be (P0=0, delta=0), got %s" % (events[0],)
    # Sum deltas from trigger (0) to first rising edge (bit 0 = 1): must equal UART bit time (521 clocks)!
    acc = 0
    found_rise = False
    for lvl, delta in events[1:]:
        acc += delta
        if lvl == 1:
            found_rise = True
            break
    assert found_rise, "never saw rising edge of bit 0 in trace: %s" % events
    assert 520 <= acc <= 522, "start-bit width measured by hardware trace was %d clocks, expected 521" % acc
    assert host.status() & ST_OVERFLOW == 0


@cocotb.test()
async def test_v2_host_read_strobe_guard(dut):
    """v2: toggling rd_strobe while HAS_BYTE == 0 must not desync nibble phase or drop a later byte."""
    host = await start(dut)
    await host.load(compile_text(ECHO).image)
    await host.select(PORT_CH3_CH2)
    assert host.status() & ST_HAS == 0

    # Toggle rd_strobe an ODD number of times (3 times) while HAS_BYTE == 0!
    for _ in range(3):
        host.rd ^= 1
        host.drive()
        await ClockCycles(dut.clk, 4)

    # Now send two bytes and verify both read back intact with no nibble swap or drop!
    for want in (0xA5, 0x3C):
        await host.write(PORT_CH3_CH2, want)
        got = await host.read(PORT_CH3_CH2)
        assert got == want, "after stray rd_strobe, expected 0x%02x got 0x%02x" % (want, got)
    assert host.status() & ST_OVERFLOW == 0


@cocotb.test()
async def test_v2_usb_tx_handshakes(dut):
    """v2: usb_ls_tx sends 1-byte ACK/NAK/STALL handshakes without CRC-16 via S[1:0] dispatch."""
    DP, DM = 0, 1
    chip = await boot(dut, "usb_ls_tx", outputs=[DP, DM], pulled_low=[DP])
    pids = (usb_ls.ACK, 0x5A, 0x1E)  # ACK, NAK, STALL
    chip.send(b"".join(bytes([1, pid]) for pid in pids))
    await chip.sent(500 * US)
    await chip.quiet([DP, DM], 30 * US, 100 * US)
    board = chip.board
    got = usb_ls.decode((board.line[DP], board.line[DM]), (board.chip[DP], board.chip[DM]), board.since)
    assert got == [bytes([p]) for p in pids], "TX handshakes went out as %s" % [g.hex() for g in got]
    await chip.finish()


@cocotb.test()
async def test_v2_usb_rx_handshakes(dut):
    """v2: usb_ls_rx accepts 1-byte ACK/NAK/STALL handshakes via 1-clock TCAM dispatch."""
    DP, DM = 0, 1
    chip = await boot(dut, "usb_ls_rx", pulled_low=[DP])
    pids = (usb_ls.ACK, 0x5A, 0x1E)
    events, t = [], now() + 1 * US
    for pid in pids:
        more, back_to_j = usb_ls.send([DP, DM], bytes([pid]), t)
        events += more
        t = back_to_j + 10 * US
    await chip.board.play(events)
    await chip.expect(ch1=bytes(pids), ch2=b"\x00\x01" * len(pids), within=50 * US)
    await chip.finish()


@cocotb.test()
async def test_v2_i2c_plain_read(dut):
    """v2: i2c_ctrl supports plain read (START, addr+R, data, STOP) via addr | 0x80 without breaking Scenario 7."""
    chip = await boot(dut, "i2c_ctrl", open_drain=[0, 1])
    i2c.Target(chip.board, 0, 1, {0x50: b"\x11\x22"}, i2c.FAST, 0)
    chip.send(bytes([0x80 | 0x50, 0, 2]))
    await chip.expect(ch1=b"\x11\x22", ch2=b"\x00", within=400 * US)
    await chip.quiet([0, 1], 10 * US, 50 * US)
    got, _ = i2c.check(chip.board, 0, 1, i2c.FAST, chip.board.since)
    assert got == ["S A1+ 11+ 22- P"], "plain read bus carried %s" % got
    await chip.finish()
