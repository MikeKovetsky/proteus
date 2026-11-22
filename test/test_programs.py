# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""What the shipped programs do beyond the 14 scenarios: other rates and modes, the gaps that closed, and the
other side or the host getting it wrong.

test_scenarios.py and protocols/ are used as they are: nothing here changes what a scenario expects. Where
bakeoff/README.md is silent, a test here says so or docs/info.md does, and none of that is the scenario's.
A test named as a closed gap has that gap's body, unchanged.
"""

import re
import zlib

import cocotb

from bench import boot, now
from host_link import PROGRAMS, ST_OVERFLOW
from bakeoff.cand_table.compiler import compile_text
from protocols import crc, eth10, i2c, onewire, ps2, spi, uart, usb_ls, ws2812
from protocols.wave import CLK, EPS, US, Wave, bits_lsb, bytes_msb
from test_scenarios import (I2C_100KHZ, I2C_FEW, ONEWIRE_RISE, SLOW_POLL, STUFFED_AT_END, i2c_retimed, i2c_run, rng,
                            some_bytes, some_phase, usb_packet)

P0, P1, P2, P3 = 0, 1, 2, 3
SPI_PINS = SCK, MOSI, MISO, CS = P0, P1, P2, P3
I2C_SCL, I2C_SDA = P0, P1
USB_PINS = DP, DM = P0, P1
DATA0, DATA1 = usb_ls.DATA0, usb_ls.DATA1


def code_of(name):
    """A program's lines without comments and spacing."""
    lines = [line.split("#")[0].split() for line in (PROGRAMS / (name + ".fsm")).read_text().splitlines()]
    return [" ".join(words) for words in lines if words]


@cocotb.test()
async def test_program_variants(dut):
    """A program shipped at several speeds is one program: the copies differ in their speed constants only."""
    for names, speeds in ((("uart_tx", "uart_tx_9600", "uart_tx_921600"), ("const BIT",)),
                          (("uart_rx", "uart_rx_9600", "uart_rx_921600"), ("const BIT",)),
                          (("i2c_ctrl", "i2c_ctrl_100k"), ("const LOW", "const HIGH"))):
        first = code_of(names[0])
        for name in names[1:]:
            other = code_of(name)
            changed = [line for line, its in zip(first, other) if line != its]
            assert len(first) == len(other) and all(line.startswith(speeds) for line in changed), (
                "%s differs from %s in more than its speed: %s" % (name, names[0], changed))


# ---- UART


@cocotb.test(timeout_time=8, timeout_unit="ms")
@cocotb.parametrize(baud=[9600, 921600])
async def test_uart_tx_rates(dut, baud):
    """uart_tx_9600 and uart_tx_921600: every edge within scenario 1's 2% of a bit, and the pin driven."""
    slow, bit = baud < 115200, 1e9 / baud
    data = b"\xa5\x00" if slow else some_bytes(rng("uart tx fast"), 32)
    chip = await boot(dut, "uart_tx_%d" % baud, outputs=[P0], poll=SLOW_POLL if slow else 64 * CLK)
    chip.send(data)
    await chip.sent(3000 * US)
    await chip.quiet([P0], 12 * bit, (len(data) + 1) * 11 * bit)
    chip.driven([P0])
    got, _ = uart.decode(chip.board.line[P0], baud, chip.board.since)
    assert got == data, "P0 carried %s, the host sent %s" % (got.hex(), data.hex())
    await chip.finish()


@cocotb.test(timeout_time=12, timeout_unit="ms")
@cocotb.parametrize(baud=[9600, 921600])
async def test_uart_rx_rates(dut, baud):
    """uart_rx_9600 and uart_rx_921600: frames back to back from a sender 2% fast and from one 2% slow."""
    slow, source = baud < 115200, rng("uart rx %d" % baud)
    n = 2 if slow else 32
    chip = await boot(dut, "uart_rx_%d" % baud, poll=SLOW_POLL if slow else 64 * CLK)
    for rate in (1 + uart.TOLERANCE, 1 - uart.TOLERANCE):
        data = some_bytes(source, n)
        events, _ = uart.send(P1, data, baud * rate, some_phase(source))
        player = chip.board.play(events)
        await chip.expect(ch1=data, within=(n + 1) * 11e9 / baud + 400 * US)
        await player
        await chip.wait(20 * US)
    await chip.finish()


UART_RECEIVERS = {  # program, baud, what the chip drives, and whether channel 2 is free to report a bad frame
    "b115200": ("uart_rx", 115200, (), True),
    "b9600": ("uart_rx_9600", 9600, (), True),
    "b921600": ("uart_rx_921600", 921600, (), True),
    "b3m": ("uart_3m", 3e6, (P0,), True),
    "four": ("extra/four_at_once", 115200, (P0, 2, 3, 4), False),
}


@cocotb.test(timeout_time=12, timeout_unit="ms")
@cocotb.parametrize(receiver=list(UART_RECEIVERS))
async def test_uart_rx_bad_stop(dut, receiver):
    """A frame whose stop bit is 0 is reported as 0x01, a long break once, and the frame one bit time after
    either is received. The scenarios send neither, and do not define the report."""
    image, baud, outputs, reports = UART_RECEIVERS[receiver]
    bit = 1e9 / baud
    chip = await boot(dut, image, outputs=outputs, poll=4 * US)
    events, end = uart.send(P1, b"\x42", baud, now() + 1 * US, stop=0)
    events.append((end, {P1: 1}))
    more, end = uart.send(P1, b"\x5a", baud, end + bit)
    events += more + [(end + bit, {P1: 0}), (end + 26 * bit, {P1: 1})]
    more, end = uart.send(P1, b"\xa5", baud, end + 27 * bit)
    player = chip.board.play(events + more)
    await chip.expect(ch1=b"\x5a\xa5", ch2=b"\x01\x01" if reports else b"", within=70 * bit + 100 * US)
    await player
    await chip.finish()


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_uart_rx_framing(dut):
    """uart_rx: a frame whose stop bit is 0 is not a byte. The next good frame still is one. An error report on
    channel 2 is allowed."""
    chip = await boot(dut, "uart_rx")
    bit = 1e9 / 115200
    broken, end = uart.send(P1, b"\x42", 115200, now() + 1 * US, stop=0)
    following, _ = uart.send(P1, b"\x5a", 115200, end + 2 * bit)
    await chip.board.play(broken + [(end, {P1: 1})] + following)
    await chip.expect(ch1=b"\x5a", within=30 * US)
    await chip.wait(10 * US)
    assert not chip.task.received[1], "channel 1 also gave %s" % chip.task.received[1].hex()


# ---- SPI
#
# protocols/spi.py judges modes 0 and 3. Modes 2 and 1 are those two with SCK upside down (the block guide's
# CPOL), so the same judge is given the clock upside down.

SPI_MODES = {0: (0, False), 1: (3, True), 2: (0, True), 3: (3, False)}  # the judge's mode, and whether SCK is inverted


class UpsideDownClock:
    """The board as a target sees it through an inverter on SCK."""

    def __init__(self, board):
        self.board = board

    def __getattr__(self, name):
        return getattr(self.board, name)

    def listen(self, callback):
        self.board.listen(lambda t, changed: callback(
            t, {pin: 1 - level if pin == SCK else level for pin, level in changed.items()}))


def spi_view(board, mode):
    """What protocols/spi.py is given for `mode`: its own mode, and the board."""
    judged, inverted = SPI_MODES[mode]
    return judged, UpsideDownClock(board) if inverted else board


def spi_clock(board, mode):
    """SCK as the judge reads it."""
    wave = board.line[SCK]
    if not SPI_MODES[mode][1]:
        return wave
    out = Wave(wave.times[0], None)
    out.times = list(wave.times)
    out.levels = [None if level is None else 1 - level for level in wave.levels]
    return out


async def spi_boot(dut, mode):
    chip = await boot(dut, "spi_ctrl_m%d" % mode, outputs=[SCK, MOSI, CS], pulled_low=[SCK] if mode < 2 else [])
    return (chip,) + spi_view(chip.board, mode)


@cocotb.test(timeout_time=1, timeout_unit="ms")
@cocotb.parametrize(mode=[1, 2])
async def test_spi_ctrl_modes_1_and_2(dut, mode):
    """spi_ctrl_m1 and spi_ctrl_m2: the two modes no scenario has, held to what scenarios 4 and 5 are."""
    source = rng("spi %d" % mode)
    out = [some_bytes(source, n) for n in (16, 1, 5)]
    back = [(some_bytes(source, len(data)), valid)
            for data, valid in zip(out, (0, spi.TARGET_VALID, spi.TARGET_VALID))]
    chip, judged, view = await spi_boot(dut, mode)
    target = spi.Target(view, SPI_PINS, judged, back)
    chip.send(b"".join([bytes([16]), out[0], b"\x00", bytes([1]), out[1], bytes([5]), out[2]]))
    await chip.expect(ch1=b"".join(data for data, _ in back), within=200 * US)
    await chip.quiet([SCK, MOSI, CS], 2 * US, 20 * US)
    board, sck = chip.board, spi_clock(chip.board, mode)
    got = spi.check_controller(sck, board.line[MOSI], board.line[CS], judged, board.since)
    chip.driven([SCK, CS])
    first = next(t for t, level in sck.changes(chip.ready) if level)
    chip.driven([MOSI], since=first - spi.DATA_MARGIN)
    assert got == out, "MOSI carried %s, the host sent %s" % ([g.hex() for g in got], [o.hex() for o in out])
    assert target.received == out, "the target read %s" % [g.hex() for g in target.received]
    assert board.chip[CS].at(now()) == 1, "CS is not driven high when idle"
    assert board.chip[SCK].at(now()) == (1 if mode > 1 else 0), "SCK is not driven to its idle level"
    await chip.finish()


class BriefTarget(spi.Target):
    """A target whose bit is also valid only `hold` ns past the edge that samples it."""

    def __init__(self, board, pins, mode, replies, hold):
        self.keep, self.bit = hold, None
        super().__init__(board, pins, mode, replies)

    def put_next_bit(self):
        self.bit = self.reply[0] if self.reply else None
        super().put_next_bit()

    def on_change(self, t, changed):
        sampled = self.bits is not None and self.cs not in changed and changed.get(self.sck) == 1
        super().on_change(t, changed)
        if sampled and self.bit is not None:
            token, wrong = self.token, 1 - self.bit
            self.board.later(self.keep, lambda: token == self.token and self.board.drive({self.miso: wrong}))


@cocotb.test(timeout_time=1, timeout_unit="ms")
@cocotb.parametrize(mode=[0, 1, 2, 3])
async def test_spi_ctrl_sampling(dut, mode):
    """MISO is read where the mode samples: a target whose data is right only from 20 ns before that edge to
    20 ns after it is in order. Reading the pad in the line that makes the edge is 3 clocks (50 ns) early."""
    data = some_bytes(rng("spi sampling"), 8)
    chip, judged, view = await spi_boot(dut, mode)
    BriefTarget(view, SPI_PINS, judged, [(data, spi.PERIOD / 2 - 20)], hold=20)
    chip.send(bytes([len(data)]) + bytes(len(data)))
    await chip.expect(ch1=data, within=50 * US)
    await chip.finish()


@cocotb.test(timeout_time=1, timeout_unit="ms")
@cocotb.parametrize(mode=[0, 1, 2, 3])
async def test_spi_ctrl_late_host(dut, mode):
    """The host is late with byte 2. The clock waits after 8 pulses with CS low, and both bytes are right. The
    scenarios' host is never late, so they do not define the wait."""
    chip, judged, view = await spi_boot(dut, mode)
    board = chip.board
    target = spi.Target(view, SPI_PINS, judged, [(b"\x96\x3c", spi.TARGET_VALID)])
    chip.send(b"\x02\xff")
    await chip.wait(20 * US)
    clocks = [t for t, level in spi_clock(board, mode).changes(board.since) if level]
    assert len(clocks) == 8, "%d clocks for 1 byte from the host" % len(clocks)
    assert board.chip[CS].at(now()) == 0, "CS rose in the middle of a transaction"
    chip.send(b"\xa5")
    await chip.expect(ch1=b"\x96\x3c", within=20 * US)
    await chip.quiet([SCK, MOSI, CS], 2 * US, 20 * US)
    sck, mosi = spi_clock(board, mode), board.line[MOSI]
    clocks = [t for t, level in sck.changes(board.since) if level]
    periods = [b - a for a, b in zip(clocks, clocks[1:])]
    assert all(abs(p - spi.PERIOD) <= spi.PERIOD_TOLERANCE + EPS for n, p in enumerate(periods) if n != 7), (
        "SCK paused other than between the two bytes: %s" % periods)
    for t in clocks:
        assert not mosi.changes(t - spi.DATA_MARGIN + EPS, t + spi.DATA_MARGIN - EPS), "MOSI moved at %.0f ns" % t
    assert target.received == [b"\xff\xa5"], "the target read %s" % [g.hex() for g in target.received]
    await chip.finish()


async def target_transaction(chip, data, idle_since):
    """One mode-0 transaction with the chip as the target. Returns what it answered and when MISO was let go."""
    events, rises, end = spi.controller_mode0(SPI_PINS, data, now() + 1 * US)
    await chip.board.play(events)
    await chip.wait(1 * US)
    got = spi.check_target(chip.board.line[MISO], chip.board.chip[MISO], events[0][0], rises, end, idle_since)
    return got, end + spi.TARGET_DISABLE


async def target_selection(chip):
    """CS low for 3 us with no clock, then high."""
    start = now() + 1 * US
    await chip.board.play([(start, {CS: 0, SCK: 0}), (start + 3 * US, {CS: 1})])
    await chip.wait(1 * US)
    assert chip.board.chip[MISO].at(start + 1 * US) is not None, "MISO is not driven when selected"
    assert chip.board.chip[MISO].at(start + 3 * US + spi.TARGET_DISABLE) is None, "MISO is driven with CS high"
    return now()


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_spi_target_selection(dut):
    """spi_target_m0: bytes queued ahead come out one transaction after another, and a selection with no
    clock spends none of them."""
    chip = await boot(dut, "spi_target_m0", outputs=[MISO], pulled_low=[SCK, MOSI])
    chip.send(b"\xa5\x3c\xc3")
    idle_since = chip.board.since
    for want in (0xA5, 0x3C, 0xC3):
        await chip.wait(3 * US)
        got, idle_since = await target_transaction(chip, b"\x81", idle_since)
        assert got == bytes([want]), "MISO carried %s, expected %02x" % (got.hex(), want)
        await chip.expect(ch1=b"\x81", within=10 * US)
        if want == 0xA5:
            idle_since = await target_selection(chip)
    await chip.finish()


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_spi_target_underrun(dut):
    """spi_target_m0: each byte clocked with none queued answers 0x00 and gives 0x01 on channel 2. A selection
    with no clock gives nothing. A byte CS cuts short is dropped, and the one after it is in step. Scenario 6
    queues all 8 bytes, so it defines none of this."""
    chip = await boot(dut, "spi_target_m0", outputs=[MISO], pulled_low=[SCK, MOSI])
    idle_since = chip.board.since
    for queued, data, flags in ((b"\x96", b"\x12\x34", 1), (b"", b"\x56", 1), (b"", b"\x78\x9a\xbc", 3),
                                (b"\x5a", b"\xde", 0)):
        chip.send(queued)
        await chip.wait(3 * US)
        got, idle_since = await target_transaction(chip, data, idle_since)
        want = queued + bytes(flags)
        assert got == want, "MISO carried %s, expected %s" % (got.hex(), want.hex())
        await chip.expect(ch1=data, ch2=b"\x01" * flags, within=10 * US)
    await target_selection(chip)
    chip.send(b"\x0f\x77")
    await chip.wait(3 * US)
    events, rises, _ = spi.controller_mode0(SPI_PINS, b"\x99", now() + 1 * US)
    cut = [(t, levels) for t, levels in events if t < rises[4] - 1]
    await chip.board.play(cut + [(rises[4], {CS: 1})])
    await chip.wait(1 * US)
    got, _ = await target_transaction(chip, b"\x66", now())
    assert got == b"\x77", "after a byte cut short MISO carried %s" % got.hex()
    await chip.expect(ch1=b"\x66", within=10 * US)
    await chip.finish()


@cocotb.test(timeout_time=1, timeout_unit="ms")
@cocotb.parametrize(after=[-100, 100, 500])
async def test_spi_target_late_first_byte(dut, after):
    """spi_target_m0: the first byte must be in channel 0 before CS falls. The host starts writing each reply
    `after` ns from that edge, and a write takes 150 ns or more, so even at -100 the byte is late, though far
    ahead of the first clock. It is not sent in its place: 0x00 and one 0x01, then one transaction behind.
    Scenario 6 queues before CS falls, so it does not define this."""
    chip = await boot(dut, "spi_target_m0", outputs=[MISO], pulled_low=[SCK, MOSI])
    idle_since = chip.board.since
    for reply, want in ((0x21, 0x00), (0x32, 0x21), (0x43, 0x32), (None, 0x43)):
        await chip.wait(3 * US)
        events, rises, end = spi.controller_mode0(SPI_PINS, b"\x81", now() + 1 * US)
        player = chip.board.play(events)
        if reply is not None:
            await chip.wait(events[0][0] + after - now())
            chip.send(bytes([reply]))
        await player
        await chip.wait(1 * US)
        got = spi.check_target(chip.board.line[MISO], chip.board.chip[MISO], events[0][0], rises, end, idle_since)
        idle_since = end + spi.TARGET_DISABLE
        assert got == bytes([want]), "MISO carried %s, expected %02x" % (got.hex(), want)
    await chip.expect(ch1=b"\x81" * 4, ch2=b"\x01", within=10 * US)
    await chip.finish()


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_spi_target_deselect_high(dut):
    """spi_target_m0: CS rises while SCK is still high. After a byte's last clock the byte taken early for the
    next one is kept, and inside a byte that byte is dropped. Scenario 6 raises CS once, with SCK low."""
    chip = await boot(dut, "spi_target_m0", outputs=[MISO], pulled_low=[SCK, MOSI])
    chip.send(b"\xa5\x3c\x96")
    for clocks, want in ((8, 0xA5), (3, 0x3C), (8, 0x96)):
        await chip.wait(3 * US)
        events, rises, _ = spi.controller_mode0(SPI_PINS, b"\x81", now() + 1 * US)
        off = rises[clocks - 1] + spi.TARGET_PERIOD / 4
        cut = [(t, levels) for t, levels in events if t < off]
        await chip.board.play(cut + [(off, {CS: 1}), (off + spi.TARGET_PERIOD, {SCK: 0})])
        got = [chip.board.line[MISO].at(t - 3 * CLK) for t in rises[:clocks]]
        assert got == [want >> (7 - n) & 1 for n in range(clocks)], "MISO carried %s for %02x" % (got, want)
        assert chip.board.chip[MISO].at(off + spi.TARGET_DISABLE) is None, "MISO is driven with CS high"
    await chip.expect(ch1=b"\x81\x81", within=10 * US)
    await chip.finish()


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_spi_ctrl_samples_early(dut):
    """spi_ctrl_m0: a target whose data is valid 20 ns before the rising edge of a 5 MHz clock is read."""
    chip = await boot(dut, "spi_ctrl_m0", outputs=[SCK, MOSI, CS], pulled_low=[SCK])
    spi.Target(chip.board, SPI_PINS, 0, [(b"\xa5", spi.PERIOD / 2 - 20)])
    chip.send(b"\x01\x00")
    await chip.expect(ch1=b"\xa5", within=20 * US)


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_underrun_is_silent(dut):
    """spi_ctrl_m0: the host is late with byte 2. The chip waits or flags it, and does not send a 0."""
    chip = await boot(dut, "spi_ctrl_m0", outputs=[SCK, MOSI, CS], pulled_low=[SCK])
    board = chip.board
    chip.send(b"\x02\xff")
    await chip.wait(20 * US)
    if chip.host.status() & ST_OVERFLOW:
        return  # flagged
    rises = sum(1 for _, level in board.line[SCK].changes(board.since) if level)
    assert rises <= 8, "%d clocks for 1 byte from the host, and no flag" % rises
    chip.send(b"\x5a")  # the late byte
    await chip.wait(20 * US)
    sampled = [board.line[MOSI].at(t) for t, level in board.line[SCK].changes(board.since) if level]
    assert len(sampled) == 16 and bytes_msb(sampled) == b"\xff\x5a", "with the late byte it clocked out %s" % sampled


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_spi_target_keeps_queued_byte(dut):
    """spi_target_m0: two bytes queued, two 1-byte transactions, and neither byte is lost as CS rises."""
    chip = await boot(dut, "spi_target_m0", outputs=[MISO], pulled_low=[SCK, MOSI])
    chip.send(b"\xa5\x3c")
    idle_since = chip.board.since
    for want in (0xA5, 0x3C):
        await chip.wait(3 * US)
        events, rises, end = spi.controller_mode0(SPI_PINS, b"\x81", now() + 1 * US)
        await chip.board.play(events)
        await chip.wait(1 * US)
        got = spi.check_target(chip.board.line[MISO], chip.board.chip[MISO], events[0][0], rises, end, idle_since)
        assert got == bytes([want]), "MISO carried %s, expected %02x" % (got.hex(), want)
        idle_since = end + spi.TARGET_DISABLE


# ---- I2C

I2C_SHIPPED = {"fast": ("i2c_ctrl", i2c.FAST), "standard": ("i2c_ctrl_100k", i2c.STANDARD)}


@cocotb.test(timeout_time=8, timeout_unit="ms")
@cocotb.parametrize(speed=list(I2C_SHIPPED), edges=["none", "sharp", "slowest"])
async def test_i2c_shipped(dut, speed, edges):
    """i2c_ctrl and i2c_ctrl_100k as shipped, no constant replaced, with no rise time, with 20 ns and with the
    longest allowed. With 20 ns they run at 400 and 100 kHz."""
    image, mode = I2C_SHIPPED[speed]
    rise = {"none": 0, "sharp": 20, "slowest": mode.tr_max}[edges]
    periods = await i2c_run(dut, image, mode, rise, I2C_FEW)
    if edges == "sharp":
        assert max(periods) <= 1e9 / mode.fmax * 1.02, "SCL is %.1f kHz, not %d" % (
            1e6 / max(periods), mode.fmax / 1000)


@cocotb.test(timeout_time=5, timeout_unit="ms")
@cocotb.parametrize(speed=list(I2C_SHIPPED))
async def test_i2c_no_read(dut, speed):
    """i2c_ctrl and i2c_ctrl_100k, a read count of 0: STOP after the write, with no repeated START, and the
    status. Scenario 7 does not define it. The chip used to read 65,536 bytes."""
    image, mode = I2C_SHIPPED[speed]
    chip = await boot(dut, image, open_drain=[P0, P1], rise={P0: 20, P1: 20}, poll=2 * US)
    i2c.Target(chip.board, P0, P1, {0x50: b"\x55", 0x2A: b"\x99"}, mode, 20, refuse={12})
    chip.send(bytes([0x50, 2, 0x10, 0xA5, 1,
                     0x2A, 2, 0x5A, 0xC3, 0,
                     0x50, 0, 0,
                     0x33, 0, 0,
                     0x50, 2, 0x09, 0x0A, 0]))  # the 12th ninth clock, after 0x09, is not acknowledged
    await chip.expect(ch1=b"\x55", ch2=b"\x00\x00\x00\x01\x01", within=3000 * US)
    await chip.quiet([P0, P1], 10 * US, 50 * US)
    got, _ = i2c.check(chip.board, P0, P1, mode, chip.board.since)
    assert got == ["S A0+ 10+ A5+ Sr A1+ 55- P", "S 54+ 5A+ C3+ P", "S A0+ P", "S 66- P", "S A0+ 09- P"], (
        "the bus carried %s" % got)
    await chip.finish()


def short_timeout():
    """i2c_ctrl giving up after one round of its 23, 1.1 ms, so that every phase can be timed out here."""
    text, count = re.subn(r"crc +init=0x3FFFFF", "crc init=0", (PROGRAMS / "i2c_ctrl.fsm").read_text())
    assert count == 1, "i2c_ctrl.fsm has no time-out to shorten"
    return compile_text(text).image


STUCK_WRITE_READ = bytes([0x50, 3, 0x10, 0x20, 0x30, 2])
I2C_STUCK = {  # the request, how many ninth clocks go by before SCL sticks, and what was read by then
    "before START": (STUCK_WRITE_READ, 0, b""),
    "in a write": (STUCK_WRITE_READ, 1, b""),
    "before Sr": (STUCK_WRITE_READ, 4, b""),
    "before a read": (STUCK_WRITE_READ, 5, b""),
    "in a read": (bytes([0x50, 0, 2]), 3, b"\xff"),  # address+W, address+R and the first byte
    "before STOP": (bytes([0x50, 1, 0x77, 0]), 2, b""),
}


@cocotb.test(timeout_time=15, timeout_unit="ms")
async def test_i2c_scl_stuck(dut):
    """i2c_ctrl, SCL held low in each phase: status 0x02, both lines released, the rest of the request
    dropped, and the request queued behind it runs as soon as SCL is free. Scenario 7 has no time-out."""
    chip = await boot(dut, short_timeout(), open_drain=[I2C_SCL, I2C_SDA], poll=5 * US)
    board = chip.board
    target = i2c.Target(board, I2C_SCL, I2C_SDA, {0x50: b"\xff" * 24}, i2c.FAST, 0)
    for phase, (request, clocks, read) in I2C_STUCK.items():
        held = 1500 * US
        if clocks:
            target.stretch = {target.acks + clocks: held}
        else:
            board.drive({I2C_SCL: 0})
            board.later(held, lambda: board.drive({I2C_SCL: None}))
        chip.send(request + bytes([0x50, 0, 1]))
        await chip.expect(ch1=read, ch2=b"\x02", within=1600 * US)
        assert board.far[I2C_SCL].at(now()) == 0, "%s: the status came only after SCL was released" % phase
        for pin in (I2C_SCL, I2C_SDA):
            assert board.chip[pin].at(now()) is None, "%s: P%d is held after the time-out" % (phase, pin)
        await chip.expect(ch1=b"\xff", ch2=b"\x00", within=1000 * US)
        await chip.quiet([I2C_SCL, I2C_SDA], 20 * US, 200 * US)
    await chip.finish()


@cocotb.test(timeout_time=40, timeout_unit="ms")
async def test_i2c_timeout_length(dut):
    """i2c_ctrl as shipped waits 25 ms for SCL and no more than 35, SMBus's limits: about 25 s of real time."""
    chip = await boot(dut, "i2c_ctrl", open_drain=[I2C_SCL, I2C_SDA], poll=50 * US)  # the test's resolution
    chip.board.drive({I2C_SCL: 0})
    chip.send(bytes([0x50, 0, 1]))
    await chip.wait(25000 * US)
    assert not chip.task.received[2], "it gave up on SCL before 25 ms"
    await chip.expect(ch2=b"\x02", within=10000 * US)
    await chip.finish()


@cocotb.test(timeout_time=3, timeout_unit="ms")
async def test_i2c_sda_stuck(dut):
    """i2c_ctrl, SDA held low where it must be high, before START and at STOP: status 0x02, not a transfer
    that reads as acknowledged. Scenario 7 does not define it."""
    chip = await boot(dut, "i2c_ctrl", open_drain=[I2C_SCL, I2C_SDA], poll=5 * US)
    board, since = chip.board, now()
    board.drive({I2C_SDA: 0})
    chip.send(bytes([0x50, 2, 0x10, 0x20, 1]))
    await chip.expect(ch2=b"\x02", within=100 * US)
    assert not any(level == 0 for _, level in board.chip[I2C_SCL].changes(since)), "it clocked a dead bus"
    board.drive({I2C_SDA: None})
    await chip.wait(5 * US)

    falls = []

    def hold_sda_for_stop(t, changed):
        if changed.get(I2C_SCL) == 0:
            falls.append(t)
            if len(falls) == 10:  # START's and nine clocks': the next one is STOP's
                board.later(100, lambda: board.drive({I2C_SDA: 0}))

    board.listen(hold_sda_for_stop)
    chip.send(bytes([0x50, 0, 0]))
    await chip.expect(ch2=b"\x02", within=100 * US)
    board.drive({I2C_SDA: None})
    await chip.finish()


@cocotb.test(timeout_time=3, timeout_unit="ms")
@cocotb.parametrize(speed=list(I2C_SHIPPED))
async def test_i2c_sda_stuck_before_sr(dut, speed):
    """i2c_ctrl and i2c_ctrl_100k, SDA held low from an ACK to past where the repeated START would be: status
    0x02 and no clock after it. With no START there, address+R would read as data and end as a success."""
    image, _ = I2C_SHIPPED[speed]
    chip = await boot(dut, image, open_drain=[I2C_SCL, I2C_SDA], poll=5 * US)
    board, falls = chip.board, []

    def hold_sda(t, changed):
        if changed.get(I2C_SCL) == 0:
            falls.append(t)
            if len(falls) in (9, 20):  # 9: the address's ACK comes next. 20: address+R's is over
                level = 0 if len(falls) == 9 else None
                board.later(100, lambda: board.drive({I2C_SDA: level}))

    board.listen(hold_sda)
    chip.send(bytes([0x50, 0, 2]))
    await chip.expect(ch2=b"\x02", within=600 * US)
    assert len(falls) == 10, "it clocked on, %d times, with no repeated START" % (len(falls) - 10)
    board.drive({I2C_SDA: None})
    chip.send(bytes([0x33, 0, 0]))  # nobody is there: the request was dropped and the bus is free
    await chip.expect(ch2=b"\x01", within=600 * US)
    await chip.finish()


@cocotb.test(timeout_time=3, timeout_unit="ms")
async def test_i2c_write_only(dut):
    """i2c_ctrl, [addr][1][byte][0]: a write with nothing to read ends with STOP and a status."""
    chip = await boot(dut, "i2c_ctrl", open_drain=[P0, P1])
    i2c.Target(chip.board, P0, P1, {0x50: b"\x11\x22"}, i2c.FAST, 0)
    chip.send(bytes([0x50, 1, 0x77, 0]))
    await chip.expect(ch2=b"\x00", within=300 * US)
    await chip.quiet([P0, P1], 10 * US, 50 * US)
    got, _ = i2c.check(chip.board, P0, P1, i2c.FAST, chip.board.since)
    assert got == ["S A0+ 77+ P"], "the bus carried %s" % got
    await chip.finish()


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_i2c_bus_free_slow_edges(dut):
    """i2c_ctrl retimed to 100 kHz on a bus with standard mode's longest rise, 1 us: tBUF is 4.7 us or more, as
    the program waits for SCL to be high before it times the bus free."""
    rise = i2c.STANDARD.tr_max
    chip = await boot(dut, i2c_retimed(**I2C_100KHZ), open_drain=[P0, P1], rise={P0: rise, P1: rise}, poll=2 * US)
    i2c.Target(chip.board, P0, P1, {0x50: b"\x11\x22"}, i2c.STANDARD, rise)
    chip.send(bytes([0x50, 1, 0x77, 1]) * 2)
    await chip.expect(ch1=b"\x11\x22", ch2=b"\x00\x00", within=2000 * US)
    await chip.quiet([P0, P1], 10 * US, 50 * US)
    i2c.check(chip.board, P0, P1, i2c.STANDARD, chip.board.since)
    await chip.finish()


# ---- WS2812

# The first WS2812B's highs, with any low short of its latch: what a frame whose host was late carries.
WS2812B_WAITED = ws2812.WS2812B._replace(low0=(700, 45 * US), low1=(300, 45 * US))


@cocotb.test(timeout_time=4, timeout_unit="ms")
async def test_ws2812_v5_frames(dut):
    """ws2812_tx, two frames back to back: the V5 reads them, whose lows are 580 ns at least and whose latch is
    280 us, and so does the first WS2812B."""
    frames = [bytes([0x0F, 0xF0, 0x55, 0x00, 0xFF, 0x81]), bytes([0xAA, 0x01, 0xFE])]
    chip = await boot(dut, "ws2812_tx", outputs=[P0], pulled_low=[P0])
    chip.send(b"".join(bytes([len(frame) // 3]) + frame for frame in frames))
    await chip.sent(1000 * US)
    await chip.quiet([P0], 350 * US, 1500 * US)  # longer than the latch, so it is after the last frame
    chip.driven([P0])
    for part in (ws2812.WS2812B_V5, ws2812.WS2812B):
        got = ws2812.decode(chip.board.line[P0], chip.board.since, now(), part)
        assert got == frames, "a %s reads %s" % (part.name, [g.hex() for g in got])
    await chip.finish()


@cocotb.test(timeout_time=3, timeout_unit="ms")
async def test_ws2812_late_host(dut):
    """ws2812_tx: a host that takes its time before the first bit breaks nothing. One that is late inside a
    frame, or late with a whole pixel, is told so with 0x01 each time. Scenario 8's host is never late."""
    chip = await boot(dut, "ws2812_tx", outputs=[P0], pulled_low=[P0], poll=5 * US)
    chip.send(b"\x02")
    await chip.wait(50 * US)
    assert not chip.task.received[2], "a frame that had not begun was reported late"
    chip.send(b"\x10")
    await chip.expect(ch2=b"\x01", within=40 * US)
    chip.send(b"\x20\x30")
    await chip.expect(ch2=b"\x01", within=100 * US)
    chip.send(b"\x40\x50\x60")
    await chip.quiet([P0], 330 * US, 1500 * US)
    got = ws2812.decode(chip.board.line[P0], chip.board.since, now(), WS2812B_WAITED)
    assert got == [b"\x10\x20\x30\x40\x50\x60"], "the late host's line carried %s" % [g.hex() for g in got]
    await chip.finish()


@cocotb.test(timeout_time=2, timeout_unit="ms")
async def test_ws2812_late_by_a_clock(dut):
    """ws2812_tx: each byte comes a clock later than the one before it, from in time to too late. A bit of more
    than 75 clocks and a 0x01 always go together."""
    chip = await boot(dut, "ws2812_tx", outputs=[P0], pulled_low=[P0])
    board, frame, rises = chip.board, some_bytes(rng("ws2812 a clock late"), 24), []

    def next_byte(t, changed):
        if changed.get(P0) == 1:
            rises.append(t)
            n, bit = divmod(len(rises), 8)
            if not bit and n < len(frame):  # a byte's last bit has begun
                board.later((47 + n) * CLK, lambda: chip.send(frame[n:n + 1]))

    board.listen(next_byte)
    chip.send(b"\x08" + frame[:1])
    await chip.until(lambda: len(rises) == 8 * len(frame), "last bit", 500 * US)
    await chip.wait(60 * US)
    got = ws2812.decode(board.line[P0], board.since, now(), WS2812B_WAITED)
    assert got == [frame], "the line carried %s" % [g.hex() for g in got]
    late = sum(abs(b - a - 75 * CLK) > CLK / 2 for a, b in zip(rises, rises[1:]))
    assert 0 < late < len(frame) - 1, "the test did not straddle the last clock: %d late" % late
    await chip.expect(ch2=b"\x01" * late, within=20 * US)
    await chip.finish()


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_ws2812_v5(dut):
    """ws2812_tx against WS2812B V5: a 0 is high 380 ns at most, and 280 us low latches."""
    chip = await boot(dut, "ws2812_tx", outputs=[P0], pulled_low=[P0])
    chip.send(b"\x01\x0f\xf0\x55")
    await chip.wait(350 * US)
    got = ws2812.decode(chip.board.line[P0], chip.board.since, now(), ws2812.WS2812B_V5)
    assert got == [b"\x0f\xf0\x55"], "the line carried %s" % [g.hex() for g in got]


# ---- 1-Wire


@cocotb.test(timeout_time=6, timeout_unit="ms")
@cocotb.parametrize(short_at=[0, 900, 1950, 2400])  # us: the start, after presence, the first and second byte read
async def test_onewire_short(dut, short_at):
    """onewire: a line shorted to ground gives 0x01 and no more, as no device does, and also when it goes short
    after the presence pulse or in the read. A byte read before that stays on channel 1. Scenario 9 has no short."""

    def plug_in(board):
        if not short_at:
            board.drive({P0: 0})
            return
        onewire.Device(board, P0, b"\xff" * 8, pdh=30 * US, pdl=240 * US, zero_low=30 * US)
        board.later(short_at * US, lambda: board.drive({P0: 0}))

    chip = await boot(dut, "onewire", open_drain=[P0], rise={P0: ONEWIRE_RISE}, far_end=plug_in, poll=SLOW_POLL)
    read = b"\xff" if short_at == 2400 else b""
    await chip.expect(ch1=read, ch2=b"\x01", within=3000 * US)
    await chip.quiet([P0], 500 * US, 1000 * US)
    await chip.finish()


ONEWIRE_PASSING = {"reset": (1, 600), "presence": (781, 1040)}  # us from reset's start: low from, until


@cocotb.test(timeout_time=6, timeout_unit="ms")
@cocotb.parametrize(short=list(ONEWIRE_PASSING))
async def test_onewire_passing_short(dut, short):
    """onewire: a short that lets go again is no presence pulse, whether the line was low as soon as reset was
    released or still low when every presence pulse is over. Only 0x01. Scenario 9 has no short."""
    low, high = ONEWIRE_PASSING[short]

    def plug_in(board):
        onewire.Device(board, P0, b"\xff" * 8, pdh=30 * US, pdl=240 * US, zero_low=30 * US)
        begun = []

        def on_reset(t, changed):
            if not begun and changed.get(P0) == 0:
                begun.append(t)
                board.later(low * US, lambda: board.drive({P0: 0}))
                board.later(high * US, lambda: board.drive({P0: None}))

        board.listen(on_reset)

    chip = await boot(dut, "onewire", open_drain=[P0], rise={P0: ONEWIRE_RISE}, far_end=plug_in, poll=SLOW_POLL)
    await chip.expect(ch2=b"\x01", within=2000 * US)
    await chip.quiet([P0], 500 * US, 1000 * US)
    await chip.finish()


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_onewire_shorted_bus(dut):
    """onewire: a line shorted to ground is no device, though all zeros have a good CRC."""
    chip = await boot(dut, "onewire", open_drain=[P0], far_end=lambda board: board.drive({P0: 0}), poll=20 * US)
    await chip.until(lambda: len(chip.task.received[2]) >= 1, "status", 7000 * US)
    await chip.wait(50 * US)
    assert chip.task.received[2][:1] == b"\x01", "a shorted bus gave %s" % chip.state()


# ---- PS/2

PS2_RECEIVERS = {  # program, clock, data, what the chip drives, where good bytes go, and what a bad frame gives
    "alone": ("ps2_rx", P0, P1, (), 1, b"\xff"),
    "four": ("extra/four_at_once", 5, 6, (P0, 2, 3, 4), 2, b""),
}


async def ps2_run(dut, receiver, broken, flags):
    """`broken(clk, data, start)` gives a broken frame's events and end. A good frame after it must be read."""
    image, clk, data, outputs, channel, flag = PS2_RECEIVERS[receiver]
    chip = await boot(dut, image, outputs=outputs, poll=SLOW_POLL)
    events, end = broken(clk, data, now() + 1 * US)
    again, after = ps2.frame(clk, data, 0x5A, end, 16.7, late=False)
    player = chip.board.play(events + again)
    got = {1: b"", 2: flag * flags}
    got[channel] += b"\x5a"
    await chip.expect(ch1=got[1], ch2=got[2], within=after - now() + 100 * US)
    await player
    await chip.finish()


@cocotb.test(timeout_time=3, timeout_unit="ms")
@cocotb.parametrize(receiver=list(PS2_RECEIVERS), fastest=[False, True])
async def test_ps2_rx_bad_frames(dut, receiver, fastest):
    """ps2_rx and its copy in four_at_once: a frame whose stop bit is 0, or whose start bit is 1, is not a good
    byte, and the frame straight after is read. ps2_rx gives 0xFF for each, which scenario 10 does not define."""
    khz = 16.7 if fastest else 10

    def broken(clk, data, start):
        events, end = ps2.frame(clk, data, 0x42, start, khz, late=False, stop=0)
        glitch = [(end + 50 * US, {clk: 0}), (end + 80 * US, {clk: None})]  # a falling clock with the data high
        return events + glitch, end + 130 * US

    await ps2_run(dut, receiver, broken, 2)


@cocotb.test(timeout_time=6, timeout_unit="ms")
@cocotb.parametrize(receiver=list(PS2_RECEIVERS), clocks=[1, 4, 10])
async def test_ps2_rx_abandoned(dut, receiver, clocks):
    """ps2_rx and its copy in four_at_once: a device abandons a frame after some of its clocks. The frame it
    sends 0.5 or 2 ms later is read whole. The 250 us time-out is the program's own figure."""
    resume = 2000 if clocks == 4 else 500

    def broken(clk, data, start):
        events, _ = ps2.frame(clk, data, 0x00, start, 16.7, late=False)
        last = events[3 * clocks - 1][0]
        return events[:3 * clocks] + [(last + 10 * US, {data: None})], last + resume * US

    await ps2_run(dut, receiver, broken, 1)


@cocotb.test(timeout_time=3, timeout_unit="ms")
@cocotb.parametrize(receiver=list(PS2_RECEIVERS))
async def test_ps2_rx_parity(dut, receiver):
    """ps2_rx and its copy in four_at_once: wrong parity is not a good byte, and the frame after it is read."""

    def broken(clk, data, start):
        return ps2.frame(clk, data, 0x42, start, 16.7, late=False, bad_parity=True)

    await ps2_run(dut, receiver, broken, 1)


@cocotb.test(timeout_time=3, timeout_unit="ms")
@cocotb.parametrize(receiver=list(PS2_RECEIVERS))
async def test_ps2_rx_clock_stops_low(dut, receiver):
    """ps2_rx and its copy in four_at_once: the clock stops low inside a frame. That times out as a clock that
    stops high does, and the frame after the line is released is read whole."""

    def broken(clk, data, start):
        events, _ = ps2.frame(clk, data, 0x00, start, 16.7, late=False)
        fell = [n for n, (_, levels) in enumerate(events) if levels.get(clk, 1) == 0][4]  # the fifth fall
        released = events[fell][0] + 500 * US
        return events[:fell + 1] + [(released, {clk: None, data: None})], released + 50 * US

    await ps2_run(dut, receiver, broken, 1)


@cocotb.test(timeout_time=2, timeout_unit="ms")
async def test_ps2_rx_framing(dut):
    """ps2_rx: a frame whose stop bit is 0 is not a good byte. The next good frame still is one."""
    chip = await boot(dut, "ps2_rx")
    broken, t = ps2.frame(P0, P1, 0x42, now() + 1 * US, 16.7, late=False, stop=0)
    following, _ = ps2.frame(P0, P1, 0x5A, t, 16.7, late=False)
    await chip.board.play(broken + following)
    await chip.expect(ch1=b"\x5a", within=30 * US)
    await chip.wait(10 * US)
    assert not chip.task.received[1], "channel 1 also gave %s" % chip.task.received[1].hex()


@cocotb.test(timeout_time=6, timeout_unit="ms")
async def test_ps2_rx_resync(dut):
    """ps2_rx: a device abandons a frame after 4 clocks. 2 ms later (the test's own figure: 20 of the longest
    clock periods), a whole frame must be read."""
    chip = await boot(dut, "ps2_rx")
    start = now() + 1 * US
    cut, _ = ps2.frame(P0, P1, 0x00, start, 16.7, late=False)
    cut = cut[:4 * 3] + [(start + 300 * US, {P1: None})]
    again, _ = ps2.frame(P0, P1, 0x5A, start + 2000 * US, 16.7, late=False)
    await chip.board.play(cut + again)
    await chip.expect(ch1=b"\x5a", within=100 * US)


# ---- low-speed USB
#
# Scenarios 11 and 12 put a CRC-16 on every packet, so every packet here is a data packet with one.


def usb_heard(board, since):
    return usb_ls.decode((board.line[DP], board.line[DM]), (board.chip[DP], board.chip[DM]), since)


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_usb_tx_waits_for_pid(dut):
    """usb_ls_tx: nothing is driven before the PID has come, and an n of 0 is ignored. Scenario 11 has neither."""
    chip = await boot(dut, "usb_ls_tx", outputs=USB_PINS, pulled_low=[DP])
    since = now()
    chip.send(b"\x01")  # the PID follows 30 us later
    await chip.wait(30 * US)
    for pin in USB_PINS:
        assert chip.board.chip[pin].at(now()) is None, "the bus is driven before the PID has come"
    chip.send(bytes([DATA0, 0, 2, DATA1, 0x42, 0, 0, 1, DATA1]))
    await chip.sent(400 * US)
    await chip.quiet(USB_PINS, 30 * US, 100 * US)
    got = usb_heard(chip.board, since)
    want = [usb_packet(DATA0, b""), usb_packet(DATA1, b"\x42"), usb_packet(DATA1, b"")]
    assert got == want, "the bus carried %s, expected %s" % ([g.hex() for g in got], [w.hex() for w in want])
    await chip.finish()


USB_CUT_SHORT = [(DATA0, 0), (DATA0, 1), (DATA1, 5)]  # the PID, and the bytes that come in time


@cocotb.test(timeout_time=1, timeout_unit="ms")
@cocotb.parametrize(cut=range(len(USB_CUT_SHORT)))
async def test_usb_tx_underrun(dut, cut):
    """usb_ls_tx: the host stops inside a packet. The packet ends in a bit-stuffing error, never under a good
    CRC, 0x01 says so, and the late bytes are not read as the next packet. Scenario 11's host is never late."""
    pid, given = USB_CUT_SHORT[cut]
    chip = await boot(dut, "usb_ls_tx", outputs=USB_PINS, pulled_low=[DP])
    since = now()
    chip.send(bytes([given + 3, pid]) + b"\xff" * given)
    await chip.expect(ch2=b"\x01", within=100 * US)
    await chip.quiet(USB_PINS, 20 * US, 100 * US)
    try:
        usb_heard(chip.board, since)
    except AssertionError as error:
        assert "seven 1s" in str(error), "the cut-short packet broke another rule: %s" % error
    else:
        raise AssertionError("the cut-short packet went out as a good one")
    since = now()
    chip.send(b"\x01\x01" + bytes([2, DATA1, 0x42]))
    await chip.sent(100 * US)
    await chip.quiet(USB_PINS, 20 * US, 100 * US)
    assert usb_heard(chip.board, since) == [usb_packet(DATA1, b"\x42")], "the packet after it is not right"
    await chip.finish()


def stretched(stretch, **how):
    """usb_ls.send through a hub that stretches the last bit before EOP by `stretch` of a bit."""

    def send(packet, start):
        events, back_to_j = usb_ls.send(USB_PINS, packet, start, **how)
        late = stretch * usb_ls.BIT / how.get("rate", 1)
        return events[:-3] + [(t + late, levels) for t, levels in events[-3:]], back_to_j + late

    return send


def unstuffed(skew=0.0):
    """A sender that leaves out every stuffed bit."""

    def send(packet, start):
        events, state, t = [], usb_ls.J, start
        for bit in [0] * 7 + [1] + bits_lsb(packet):
            if not bit:
                state = usb_ls.K if state == usb_ls.J else usb_ls.J
                rising, falling = (DP, DM) if state == usb_ls.K else (DM, DP)
                if skew:
                    events += [(t - skew / 2, {falling: 0}), (t + skew / 2, {rising: 1})]
                else:
                    events.append((t, {falling: 0, rising: 1}))
            t += usb_ls.BIT
        back_to_j = t + 2 * usb_ls.BIT
        events += [(t, {DP: 0, DM: 0}), (back_to_j, {DM: 1}), (back_to_j + usb_ls.BIT, {DP: None, DM: None})]
        return events, back_to_j

    return send


async def usb_rx_run(dut, packets, source):
    """`packets` are (bytes, who sends them, what channel 1 gets, good or not), 2 bit times apart."""
    chip = await boot(dut, "usb_ls_rx", pulled_low=[DP])
    events, ch1, ch2, t = [], b"", b"", some_phase(source)
    for packet, send, read, good in packets:
        more, back_to_j = send(packet, t)
        events += more
        t = back_to_j + usb_ls.PACKET_GAP + source.uniform(1, CLK)
        ch1 += read
        ch2 += bytes([0 if good else 1, len(read)])
    player = chip.board.play(events)
    await chip.expect(ch1=ch1, ch2=ch2, within=t - now() + 50 * US)
    await player
    await chip.finish()


def ends_in_ones(data, count):
    bits = usb_ls.stuff([0] * 7 + [1] + bits_lsb(usb_packet(DATA0, data)))
    return bits[-count:] == [1] * count and bits[-count - 1] == 0


FIVE_ONES_AT_END = next(bytes([a, b]) for a in range(256) for b in range(256) if ends_in_ones(bytes([a, b]), 5))


@cocotb.test(timeout_time=1, timeout_unit="ms")
@cocotb.parametrize(stretch=[25, 55, 80, 100])
async def test_usb_rx_stretch(dut, stretch):
    """usb_ls_rx: hubs can stretch the last bit before EOP by up to a bit, and a receiver must accept it
    (7.1.9.1). So too at both rate limits and with the longest SE0 inside transitions, after a stuffed bit,
    and when the stretch looks like a sixth 1."""
    tolerance, packets = usb_ls.RATE_TOLERANCE, []
    for rate, skew in ((1, 0), (1 + tolerance, 0), (1 - tolerance, 0), (1 + tolerance, usb_ls.TRANSITION_SE0)):
        send = stretched(stretch / 100, rate=rate, skew=skew)
        for packet in (usb_packet(DATA0, b"\x12\x34"), usb_packet(DATA0, STUFFED_AT_END),
                       usb_packet(DATA0, FIVE_ONES_AT_END), usb_packet(DATA1, b"")):
            packets.append((packet, send, packet, True))
    await usb_rx_run(dut, packets, rng("usb dribble %d" % stretch))


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_usb_rx_seven_ones(dut):
    """usb_ls_rx: seven 1s in a row void the packet (7.1.9), CRC right or not, with the longest SE0 inside
    transitions too, and the packet after it is read. Scenario 12 has no stuffing error. The chip reports it
    as 0x01 and pushes no more of that packet's bytes."""
    data = usb_packet(DATA1, b"\x5a")
    seven = usb_packet(DATA0, b"\xff")
    packets = [
        (seven, unstuffed(), bytes([DATA0]), False),
        (data, stretched(0), data, True),
        (seven, unstuffed(usb_ls.TRANSITION_SE0), bytes([DATA0]), False),
        (data, stretched(0), data, True),
    ]
    await usb_rx_run(dut, packets, rng("usb stuff error"))


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_usb_rx_not_whole(dut):
    """usb_ls_rx: a hub adds one bit at most, so two are bad. A right CRC part of the way through a packet does
    not make the bytes after it good: scenario 12's CRC is the whole packet's."""
    data = usb_packet(DATA0, b"\x12\x34")
    longer = data + b"\x5a"
    packets = [
        (data, stretched(2), data, False),
        (longer, stretched(0), longer, False),
        (data, stretched(0), data, True),
    ]
    await usb_rx_run(dut, packets, rng("usb not whole"))


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_usb_rx_no_eop(dut):
    """usb_ls_rx: a glitch to K with no EOP after it is one bad packet, and does not swallow the next one.
    Scenario 12 does not define it."""
    chip = await boot(dut, "usb_ls_rx", pulled_low=[DP])
    t = now() + 1 * US
    glitch = [(t, {DP: 1, DM: 0}), (t + usb_ls.BIT, {DP: None, DM: None})]
    data = usb_packet(DATA1, b"\x5a")
    events, _ = usb_ls.send(USB_PINS, data, t + 25 * US)
    player = chip.board.play(glitch + events)
    await chip.expect(ch1=data, ch2=bytes([1, 0, 0, len(data)]), within=100 * US)
    await player
    await chip.finish()


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_usb_rx_dribble(dut):
    """usb_ls_rx: hubs can stretch the last bit before EOP, and a receiver must accept up to six whole bit
    times with no transition there (7.1.9.1). Here one bit becomes two."""
    chip = await boot(dut, "usb_ls_rx", pulled_low=[DP])
    packet = bytes([DATA0]) + crc.with_crc(crc.CRC16_USB, b"\x12\x34")
    events, _ = usb_ls.send(USB_PINS, packet, now() + 1 * US)
    events = events[:-3] + [(t + usb_ls.BIT, levels) for t, levels in events[-3:]]
    await chip.board.play(events)
    await chip.expect(ch1=packet, ch2=bytes([0, len(packet)]), within=20 * US)


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_usb_rx_stuff_error(dut):
    """usb_ls_rx: seven 1s in a row is a bit-stuffing error and the packet is void (7.1.9), CRC right or not."""
    chip = await boot(dut, "usb_ls_rx", pulled_low=[DP])
    packet = bytes([DATA0]) + crc.with_crc(crc.CRC16_USB, b"\xff")
    events, state, t = [], usb_ls.J, now() + 1 * US
    for bit in [0] * 7 + [1] + bits_lsb(packet):  # NRZI with no stuffing
        if not bit:
            state = usb_ls.K if state == usb_ls.J else usb_ls.J
            events.append((t, {DP: state[0], DM: state[1]}))
        t += usb_ls.BIT
    events += [(t, {DP: 0, DM: 0}), (t + 2 * usb_ls.BIT, {DM: 1}), (t + 3 * usb_ls.BIT, {DP: None, DM: None})]
    sound = bytes([DATA0]) + crc.with_crc(crc.CRC16_USB, b"\x12")
    following, _ = usb_ls.send(USB_PINS, sound, t + 8 * usb_ls.BIT)
    await chip.board.play(events + following)
    await chip.until(lambda: len(chip.task.received[2]) >= 2, "status", 20 * US)
    assert chip.task.received[2][0] != 0, "a stuffing error was reported as a good packet"
    await chip.until(lambda: len(chip.task.received[2]) >= 4, "status for the packet after it", 20 * US)
    assert chip.task.received[2][2:4] == bytes([0, len(sound)]), "the good packet after it was not accepted"


# ---- 10 Mbit Ethernet


@cocotb.test(timeout_time=2, timeout_unit="ms")
@cocotb.parametrize(given=[0, 1, 30, 59])
async def test_eth10_tx_late_host(dut, given):
    """eth10_tx: the host stops inside a 60-byte frame. What came goes out with every bit of its FCS wrong,
    0x01 says so, and the late bytes are not read as the next frame's length. Scenario 13's host is never late."""
    frame, after = bytes(range(1, 61)), bytes(range(100, 160))
    chip = await boot(dut, "eth10_tx", outputs=[P0, P1], pulled_low=[P0, P1])
    chip.send(b"\x3c\x00" + frame[:given])
    await chip.expect(ch2=b"\x01", within=100 * US)
    chip.send(frame[given:] + b"\x3c\x00" + after)
    await chip.sent(300 * US)
    await chip.quiet([P0, P1], 15 * US, 150 * US)
    got = eth10.decode((chip.board.line[P0], chip.board.line[P1]), chip.board.since)
    wrong = bytes(255 - byte for byte in zlib.crc32(frame[:given]).to_bytes(4, "little"))
    want = [frame[:given] + wrong, crc.with_crc(crc.CRC32_ETHERNET, after)]
    assert got == want, "the line carried %s" % [sent.hex() for sent in got]
    await chip.finish()


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_eth10_tx_underrun(dut):
    """eth10_tx: the host stops after 30 of 60 bytes. A cut-short frame must go out broken or flagged."""
    chip = await boot(dut, "eth10_tx", outputs=[P0, P1], pulled_low=[P0, P1])
    chip.send(b"\x3c\x00" + bytes(range(1, 31)))
    await chip.wait(100 * US)
    if chip.host.status() & ST_OVERFLOW:
        return  # flagged
    board = chip.board
    assert board.line[P0].changes(board.since), "nothing went out, and no flag"
    assert (board.line[P0].at(now()), board.line[P1].at(now())) == (0, 0), "the pair never went back to idle"
    try:
        frames = eth10.decode((board.line[P0], board.line[P1]), board.since)
    except AssertionError:
        return  # broken on the wire, which every receiver drops
    for sent in frames:
        valid = sent[-4:] == zlib.crc32(sent[:-4]).to_bytes(4, "little")
        assert not valid, "30 bytes became a valid frame: %s" % sent.hex()


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_eth10_tx_no_length(dut):
    """eth10_tx: a length of 0 is no frame and is ignored. The chip used to take the next 65,536 bytes as its
    frame. Scenario 13's frames are 60 bytes or more."""
    frame = bytes(range(1, 61))
    chip = await boot(dut, "eth10_tx", outputs=[P0, P1], pulled_low=[P0, P1])
    chip.send(b"\x00\x00\x00\x00\x3c\x00" + frame)
    await chip.sent(150 * US)
    await chip.quiet([P0, P1], 15 * US, 150 * US)
    got = eth10.decode((chip.board.line[P0], chip.board.line[P1]), chip.board.since)
    assert got == [crc.with_crc(crc.CRC32_ETHERNET, frame)], "the line carried %s" % [sent.hex() for sent in got]
    await chip.finish()
