# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""What the shipped programs do beyond the 14 scenarios: other rates and modes, the gaps that closed, and the
other side or the host getting it wrong.

test_scenarios.py and protocols/ are used as they are: nothing here changes what a scenario expects. Where
bakeoff/README.md is silent, a test here says so or docs/info.md does, and none of that is the scenario's.
A test named as a closed gap has that gap's body, unchanged.
"""

import re

import cocotb

from bench import boot, now
from host_link import PROGRAMS, ST_OVERFLOW
from bakeoff.cand_table.compiler import compile_text
from protocols import i2c, onewire, ps2, spi, uart, ws2812
from protocols.wave import CLK, EPS, US, Wave, bytes_msb
from test_scenarios import (ACKED, ADDRESS_W, I2C_100KHZ, I2C_FEW, ONEWIRE_RISE, SLOW_POLL, i2c_retimed, i2c_run,
                            rng, some_bytes, some_phase)

P0, P1, P2, P3 = 0, 1, 2, 3
SPI_PINS = SCK, MOSI, MISO, CS = P0, P1, P2, P3
I2C_SCL, I2C_SDA = P0, P1


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
    chip = await boot(dut, "i2c_ctrl", open_drain=[I2C_SCL, I2C_SDA], poll=250 * US)
    chip.board.drive({I2C_SCL: 0})
    chip.send(bytes([0x50, 0, 1]))
    await chip.wait(24900 * US)
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
@cocotb.parametrize(short_at=[0, 900, 1950])  # us: from the start, in the rest of the presence slot, in the read
async def test_onewire_short(dut, short_at):
    """onewire: a line shorted to ground gives only 0x01, as no device does, and also when it goes short after
    the presence pulse or in the first byte read. Scenario 9 has no short."""

    def plug_in(board):
        if not short_at:
            board.drive({P0: 0})
            return
        onewire.Device(board, P0, b"\xff" * 8, pdh=30 * US, pdl=240 * US, zero_low=30 * US)
        board.later(short_at * US, lambda: board.drive({P0: 0}))

    chip = await boot(dut, "onewire", open_drain=[P0], rise={P0: ONEWIRE_RISE}, far_end=plug_in, poll=SLOW_POLL)
    await chip.expect(ch2=b"\x01", within=3000 * US)
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
