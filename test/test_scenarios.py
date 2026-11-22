# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The 14 bake-off scenarios (bakeoff/README.md) end to end through the 24 pins.

Each test loads the shipped program through the loader, plays the host through the nibble link and plays the
far end on uio. The judges are the decoders, far ends and limits in protocols/, written from each protocol's
definition: the bake-off harness and the golden model are not used, and nothing inside the chip is read, so
the same tests run on the routed netlist (GATES=yes).

Time is absolute, on a chip at 60 MHz (bench.py). Where a protocol allows a range, the far end uses its ends.
What the programs cannot do is in known_gaps.py.
"""

import random
import re
import zlib

import cocotb

from bench import boot, now
from host_link import PROGRAMS
from bakeoff.cand_table.compiler import compile_text
from protocols import crc, eth10, i2c, onewire, ps2, spi, uart, usb_ls, ws2812
from protocols.wave import CLK, US, bits_lsb

P0, P1, P2, P3 = 0, 1, 2, 3
SPI_PINS = SCK, MOSI, MISO, CS = P0, P1, P2, P3
I2C_SCL, I2C_SDA = P0, P1
USB_PINS = DP, DM = P0, P1
SLOW_POLL = 20 * US  # for protocols whose bytes are tens of microseconds apart
DATA0, DATA1 = 0xC3, 0x4B  # USB PIDs


def rng(name):
    return random.Random("protoemu " + name)  # the same bytes and phases on every run


def some_bytes(source, n):
    """n bytes that always include the awkward ones."""
    data = [0x00, 0xFF, 0x55, 0xAA, 0x80, 0x01][:n]
    data += [source.randrange(256) for _ in range(n - len(data))]
    source.shuffle(data)
    return bytes(data)


def some_phase(source):
    """A far end's clock has nothing to do with the chip's."""
    return now() + 1 * US + source.uniform(0, CLK)


@cocotb.test()
async def test_crc_references(dut):
    """The reference CRCs give the catalogue's check values and published codewords, and agree with zlib."""
    for params, check in crc.CHECKS:
        assert crc.crc(params, crc.CHECK_INPUT) == check, "CRC-%d misses its check value" % params[0]
    for params, word in crc.CODEWORDS:
        size = params[0] // 8
        assert crc.with_crc(params, word[:-size]) == word, "CRC-%d misses %s" % (params[0], word.hex())
    data = some_bytes(rng("crc"), 300)
    assert crc.crc(crc.CRC32_ETHERNET, data) == zlib.crc32(data)


# ---- UART


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_scenario_uart_tx(dut):
    """1. uart_tx: 16 bytes as 8N1 at 115200 baud, every edge within 2% of a bit."""
    baud, data = 115200, some_bytes(rng("uart_tx"), 16)
    bit = 1e9 / baud
    chip = await boot(dut, "uart_tx", outputs=[P0], poll=SLOW_POLL)
    chip.send(data)
    await chip.sent(3000 * US)
    await chip.quiet([P0], 12 * bit, 1000 * US)
    got, _ = uart.decode(chip.board.line[P0], baud, chip.board.since)
    assert got == data, "P0 carried %s, the host sent %s" % (got.hex(), data.hex())
    assert chip.board.chip[P0].at(now()) == 1, "TX is not driven high when idle"
    await chip.finish()


@cocotb.test(timeout_time=8, timeout_unit="ms")
async def test_scenario_uart_rx(dut):
    """2. uart_rx: 16 bytes back to back at 115200 baud, from a sender 2% fast and from one 2% slow."""
    source = rng("uart_rx")
    chip = await boot(dut, "uart_rx", poll=SLOW_POLL)
    for rate in (1.02, 0.98):
        data = some_bytes(source, 16)
        events, _ = uart.send(P1, data, 115200 * rate, some_phase(source))
        player = chip.board.play(events)
        await chip.expect(ch1=data, within=2000 * US)
        await player
        await chip.wait(20 * US)
    await chip.finish()


@cocotb.test(timeout_time=2, timeout_unit="ms")
async def test_scenario_uart_3m(dut):
    """3. uart_3m: 16 bytes each way at once at 3 Mbaud, from a sender 2% fast and from one 2% slow."""
    source = rng("uart_3m")
    chip = await boot(dut, "uart_3m", outputs=[P0])
    for rate in (1.02, 0.98):
        since, out, back = now(), some_bytes(source, 16), some_bytes(source, 16)
        events, end = uart.send(P1, back, 3e6 * rate, some_phase(source))
        first = events[0][0]
        player = chip.board.play(events)
        chip.send(out)
        await chip.expect(ch1=back, within=200 * US)
        await player
        await chip.quiet([P0], 5 * US, 200 * US)
        got, span = uart.decode(chip.board.line[P0], 3e6, since)
        assert got == out, "P0 carried %s, the host sent %s" % (got.hex(), out.hex())
        overlap = min(end, span[1]) - max(first, span[0])
        assert overlap > 0.75 * (end - first), "the two directions did not run at once"
    await chip.finish()


# ---- SPI


async def spi_controller(dut, mode):
    """Transactions of 16, 1 and 5 bytes back to back, and an empty request between. The target answers at
    once, as in the scenario, and then 50 ns after the edge that launches each bit, as a real part does."""
    source = rng("spi %d" % mode)
    out = [some_bytes(source, n) for n in (16, 1, 5)]
    back = [(some_bytes(source, len(data)), valid) for data, valid in zip(out, (0, 50, 50))]
    chip = await boot(dut, "spi_ctrl_m%d" % mode, outputs=[SCK, MOSI, CS],
                      pulled_low=[SCK] if spi.IDLE[mode] == 0 else [])
    target = spi.Target(chip.board, SPI_PINS, mode, back)
    chip.send(b"".join([bytes([16]), out[0], b"\x00", bytes([1]), out[1], bytes([5]), out[2]]))
    await chip.expect(ch1=b"".join(data for data, _ in back), within=200 * US)
    await chip.quiet([SCK, MOSI, CS], 2 * US, 20 * US)
    line = chip.board.line
    got = spi.check_controller(line[SCK], line[MOSI], line[CS], mode, chip.board.since)
    assert got == out, "MOSI carried %s, the host sent %s" % ([g.hex() for g in got], [o.hex() for o in out])
    assert target.received == out, "the target read %s" % [g.hex() for g in target.received]
    assert chip.board.chip[CS].at(now()) == 1, "CS is not driven high when idle"
    assert chip.board.chip[SCK].at(now()) == spi.IDLE[mode], "SCK is not driven to its idle level"
    await chip.finish()


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_scenario_spi_ctrl_m0(dut):
    """4. spi_ctrl_m0: the controller in mode 0 at 5 MHz."""
    await spi_controller(dut, 0)


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_scenario_spi_ctrl_m3(dut):
    """5. spi_ctrl_m3: the controller in mode 3 at 5 MHz."""
    await spi_controller(dut, 3)


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def test_scenario_spi_target_m0(dut):
    """6. spi_target_m0: 8 bytes each way from a controller at 2 MHz, then 8 more the host queued afterwards."""
    source = rng("spi target")
    chip = await boot(dut, "spi_target_m0", outputs=[MISO], pulled_low=[SCK, MOSI])
    idle_since = chip.board.since
    for _ in range(2):
        out, back = some_bytes(source, 8), some_bytes(source, 8)
        chip.send(back)
        await chip.wait(3 * US)  # the host fills channel 0 before CS falls
        events, rises, end = spi.controller_mode0(SPI_PINS, out, some_phase(source))
        player = chip.board.play(events)
        await chip.expect(ch1=out, within=100 * US)
        await player
        await chip.wait(1 * US)
        got = spi.check_target(chip.board.line[MISO], chip.board.chip[MISO], events[0][0], rises, end, idle_since)
        assert got == back, "MISO carried %s, the host queued %s" % (got.hex(), back.hex())
        idle_since = end + spi.TARGET_DISABLE
    assert not chip.board.chip[MISO].changes(idle_since), "MISO is driven while CS is high"
    await chip.finish()


# ---- I2C

ACKED, ADDRESS_W, DATA, ADDRESS_R = None, "address+W", "data", "address+R"  # where a transaction is refused
I2C_READS = {0x50: b"\x55\x00\xff\x81\x3c\x7e\xc3\x96\x0f", 0x2A: b"\x99\x24\xe0"}
# (address, bytes written, bytes read, where it is refused)
I2C_ALL = (
    (0x50, b"\x10\xa5\x00\xff", 4, ACKED),
    (0x2A, b"\x81", 1, ACKED),
    (0x50, b"", 3, ACKED),
    (0x33, b"\x01\x02\x03", 2, ADDRESS_W),  # nobody lives there: the scenario's run B
    (0x2A, b"\x07\x08", 1, DATA),
    (0x50, b"\x04", 2, ADDRESS_R),
    (0x2A, b"", 2, ACKED),
)
I2C_FEW = ((0x50, b"\x10\xa5", 2, ACKED), (0x33, b"\x01", 2, ADDRESS_W), (0x2A, b"", 1, ACKED))


async def i2c_run(dut, image, mode, rise, requests):
    """Back-to-back requests. The target stretches for 20 us after the ACK before the first repeated START
    and after the first ACK the controller gives."""
    left = {addr: list(data) for addr, data in I2C_READS.items()}
    request, read, statuses, expected = bytearray(), bytearray(), bytearray(), []
    refuse, clocks = [], 0  # ninth clocks so far
    for addr, written, nread, refused in requests:
        request += bytes([addr, len(written)]) + written + bytes([nread])
        words = ["S", "%02X%s" % (addr << 1, "-" if refused == ADDRESS_W else "+")]
        clocks += 1
        if refused != ADDRESS_W:
            for byte in written:
                clocks += 1
                words.append("%02X%s" % (byte, "-" if refused == DATA else "+"))
                if refused == DATA:
                    refuse.append(clocks)
                    break
            if refused != DATA:
                clocks += 1
                words += ["Sr", "%02X%s" % (addr << 1 | 1, "-" if refused == ADDRESS_R else "+")]
                if refused == ADDRESS_R:
                    refuse.append(clocks)
                else:
                    for n in range(nread):
                        byte = left[addr].pop(0)
                        read.append(byte)
                        clocks += 1
                        words.append("%02X%s" % (byte, "+" if n + 1 < nread else "-"))
        statuses.append(0 if refused == ACKED else 1)
        expected.append(" ".join(words + ["P"]))
    nwritten = len(requests[0][1])
    stretch = {1 + nwritten: 20 * US, 3 + nwritten: 20 * US}

    chip = await boot(dut, image, open_drain=[I2C_SCL, I2C_SDA], rise={I2C_SCL: rise, I2C_SDA: rise}, poll=2 * US)
    i2c.Target(chip.board, I2C_SCL, I2C_SDA, I2C_READS, mode, rise, stretch, refuse)
    chip.send(request)
    await chip.expect(ch1=read, ch2=statuses, within=3000 * US)
    await chip.quiet([I2C_SCL, I2C_SDA], 10 * US, 50 * US)
    for pin in (I2C_SCL, I2C_SDA):
        assert chip.board.chip[pin].at(now()) is None, "P%d is held after STOP" % pin
        pulls = [level for _, level in chip.board.far[pin].changes(chip.board.since) if level == 0]
        assert len(pulls) >= 2, "the target never drove P%d" % pin
    got, periods = i2c.check(chip.board, I2C_SCL, I2C_SDA, mode, chip.board.since)
    assert got == expected, "the bus carried %s, expected %s" % (got, expected)
    await chip.finish()
    assert periods, "no SCL period was measured"
    return periods


def i2c_retimed(low, high):
    """The shipped program with its two timing constants replaced, and nothing else."""
    text = (PROGRAMS / "i2c_ctrl.fsm").read_text()
    for name, value in (("LOW", low), ("HIGH", high)):
        text, count = re.subn(r"(?m)^(\s*const %s\s*=\s*)\d+" % name, r"\g<1>%d" % value, text)
        assert count == 1, "i2c_ctrl.fsm has no const %s to retime" % name
    return compile_text(text).image


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_scenario_i2c_ctrl(dut):
    """7. i2c_ctrl as shipped, on a fast-mode bus with sharp edges: write then read, stretching, a NACK at
    every place one can come, and STOP to START as fast as the chip goes."""
    periods = await i2c_run(dut, "i2c_ctrl", i2c.FAST, 20, I2C_ALL)
    for period in periods:
        assert 150 * CLK - 0.01 <= period <= 200 * CLK + 0.01, "SCL period %.1f ns, not 150 to 200 clocks" % period


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_i2c_fast_slowest_edges(dut):
    """i2c_ctrl as shipped on a fast-mode bus with the longest rise time allowed, 300 ns."""
    await i2c_run(dut, "i2c_ctrl", i2c.FAST, i2c.FAST.tr_max, I2C_FEW)


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_i2c_400khz(dut):
    """i2c_ctrl retimed by its two constants to a full 400 kHz (as shipped it runs at about 350 kHz)."""
    periods = await i2c_run(dut, i2c_retimed(low=80, high=62), i2c.FAST, 20, I2C_FEW)
    assert max(periods) <= 1e9 / 400e3 * 1.02, "SCL is %.1f kHz, not 400" % (1e6 / max(periods))


@cocotb.test(timeout_time=8, timeout_unit="ms")
async def test_i2c_100khz(dut):
    """i2c_ctrl retimed by its two constants to standard mode's 100 kHz, every standard-mode limit kept."""
    periods = await i2c_run(dut, i2c_retimed(low=310, high=285), i2c.STANDARD, 20, I2C_FEW)
    assert max(periods) <= 1e9 / 100e3 * 1.02, "SCL is %.1f kHz, not 100" % (1e6 / max(periods))


# ---- WS2812, 1-Wire, PS/2


@cocotb.test(timeout_time=2, timeout_unit="ms")
async def test_scenario_ws2812_tx(dut):
    """8. ws2812_tx: 8 pixels, then 1 more queued behind them, each frame latched by 50 us low."""
    source = rng("ws2812")
    frames = [some_bytes(source, 24), some_bytes(source, 3)]
    chip = await boot(dut, "ws2812_tx", outputs=[P0], pulled_low=[P0])
    chip.send(b"".join(bytes([len(frame) // 3]) + frame for frame in frames))
    await chip.sent(1000 * US)
    await chip.quiet([P0], ws2812.WS2812B.reset + 5 * US, 300 * US)
    got = ws2812.decode(chip.board.line[P0], chip.board.since, now())
    assert got == frames, "P0 carried %s, the host sent %s" % ([g.hex() for g in got], [f.hex() for f in frames])
    assert chip.board.chip[P0].at(now()) == 0, "the line is not held low when idle"
    await chip.finish()


async def onewire_run(dut, rom, pdh=None, pdl=None, zero_low=None):
    devices = []

    def plug_in(board):
        if rom:
            devices.append(onewire.Device(board, P0, rom, pdh, pdl, zero_low))

    chip = await boot(dut, "onewire", open_drain=[P0], rise={P0: 1 * US}, far_end=plug_in, poll=SLOW_POLL)
    if rom:
        good = crc.crc(crc.CRC8_MAXIM, rom[:7]) == rom[7]
        await chip.expect(ch1=rom, ch2=bytes([0, 0 if good else 1]), within=7000 * US)
    else:
        await chip.expect(ch2=b"\x01", within=1000 * US)
    await chip.quiet([P0], 100 * US if rom else 500 * US, 1500 * US)  # no slot follows
    board = chip.board
    written = onewire.check_controller(board.line[P0], board.chip[P0], board.since, 64 if rom else 0)
    assert written == (b"\x33" if rom else b""), "the chip wrote %s" % written.hex()
    assert board.chip[P0].at(now()) is None, "DQ is held at the end"
    await chip.finish()


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_scenario_onewire_good_crc(dut):
    """9A. onewire: a published ROM. Presence is the earliest and shortest allowed, and a 0 is held only
    for the 15 us that tRDV promises."""
    await onewire_run(dut, crc.CODEWORDS[0][1], pdh=15 * US, pdl=60 * US, zero_low=15 * US)


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def test_scenario_onewire_bad_crc(dut):
    """9B. onewire: a ROM with one bit wrong. Presence is the latest and longest allowed, and a 0 is held
    for the whole slot."""
    rom = bytearray(crc.CODEWORDS[1][1])
    rom[3] ^= 0x10
    await onewire_run(dut, bytes(rom), pdh=60 * US, pdl=240 * US, zero_low=60 * US)


@cocotb.test(timeout_time=3, timeout_unit="ms")
async def test_scenario_onewire_no_device(dut):
    """9C. onewire: nobody answers the reset."""
    await onewire_run(dut, None)


@cocotb.test(timeout_time=12, timeout_unit="ms")
async def test_scenario_ps2_rx(dut):
    """10. ps2_rx: 8 frames, one with wrong parity, at 12.5 kHz and at both ends of 10 to 16.7 kHz."""
    data = bytes([0x1C, 0xF0, 0x00, 0x5A, 0xFF, 0x80, 0x01, 0xAA])
    rates = (12.5, 12.5, 12.5, 12.5, 12.5, 16.7, 10.0, 12.5)
    chip = await boot(dut, "ps2_rx", poll=SLOW_POLL)
    events, t = [], some_phase(rng("ps2"))
    for n, (byte, khz) in enumerate(zip(data, rates)):
        more, t = ps2.frame(P0, P1, byte, t, khz, late=n % 2 == 1, bad_parity=n == 3)
        events += more
    player = chip.board.play(events)
    await chip.expect(ch1=data[:3] + data[4:], ch2=b"\xff", within=9000 * US)
    await player
    await chip.finish()


# ---- low-speed USB


def usb_packet(pid, data):
    return bytes([pid]) + crc.with_crc(crc.CRC16_USB, data)


def ends_stuffed(data):
    bits = [0] * 7 + [1] + bits_lsb(usb_packet(DATA0, data))
    return len(usb_ls.stuff(bits)) > len(usb_ls.stuff(bits[:-1])) + 1


# Its CRC ends in six 1s, so the last thing before EOP is a stuffed bit (USB 2.0 7.1.9).
STUFFED_AT_END = next(bytes([a, b]) for a in range(256) for b in range(256) if ends_stuffed(bytes([a, b])))


@cocotb.test(timeout_time=2, timeout_unit="ms")
async def test_scenario_usb_ls_tx(dut):
    """11. usb_ls_tx: the scenario's three packets, then published CRC-16 codewords, the CRC's check string
    and a packet whose last bit before EOP is a stuffed one."""
    published = [word for params, word in crc.CODEWORDS if params == crc.CRC16_USB]
    payloads = [b"", b"\xff" * 8, some_bytes(rng("usb tx"), 5)]
    payloads += [word[:-2] for word in published] + [crc.CHECK_INPUT, STUFFED_AT_END]
    want = [usb_packet(DATA1 if n % 2 else DATA0, data) for n, data in enumerate(payloads)]
    chip = await boot(dut, "usb_ls_tx", outputs=USB_PINS, pulled_low=[DP])
    chip.send(b"".join(bytes([1 + len(data), packet[0]]) + data for data, packet in zip(payloads, want)))
    await chip.sent(1000 * US)
    await chip.quiet(USB_PINS, 10 * US, 300 * US)
    board = chip.board
    got = usb_ls.decode((board.line[DP], board.line[DM]), (board.chip[DP], board.chip[DM]), board.since)
    assert got == want, "the bus carried %s, expected %s" % ([g.hex() for g in got], [w.hex() for w in want])
    assert [g[1:] for g in got[3:3 + len(published)]] == published, "not the published CRC-16 bytes"
    for pin in USB_PINS:
        assert board.chip[pin].at(now()) is None, "the bus is still driven at the end"
    await chip.finish()


@cocotb.test(timeout_time=2, timeout_unit="ms")
async def test_scenario_usb_ls_rx(dut):
    """12. usb_ls_rx: the scenario's four packets with random jitter, then the specification's worst: most
    jitter at both rate limits, shortest EOP, longest SE0 inside a transition, a stuffed bit before EOP, and
    packets 2 bit times apart."""
    source = rng("usb rx")
    chip = await boot(dut, "usb_ls_rx", pulled_low=[DP])
    tolerance, paired = usb_ls.RATE_TOLERANCE, usb_ls.RECEIVER_JITTER_PAIRED / 2

    def wander(n):
        return source.uniform(-paired, paired)  # so neither figure can be exceeded

    def alternate(n):
        return (0.5 if n % 2 else -0.5) * usb_ls.RECEIVER_JITTER_NEXT  # the most from one edge to the next

    bad = bytearray(usb_packet(DATA1, some_bytes(source, 4)))
    bad[-1] ^= 0x04
    packets = (
        (bytes([DATA0]) + crc.CODEWORDS[2][1], dict(rate=1 + tolerance, jitter=wander)),
        (usb_packet(DATA1, b"\xff" * 8), dict(rate=1 - tolerance, jitter=wander)),
        (usb_packet(DATA0, some_bytes(source, 5)), dict(jitter=wander)),
        (bytes(bad), dict(jitter=wander)),
        (usb_packet(DATA0, some_bytes(source, 8)), dict(rate=1 + tolerance, jitter=alternate)),
        (usb_packet(DATA1, some_bytes(source, 8)), dict(rate=1 - tolerance, jitter=alternate)),
        (usb_packet(DATA0, b""), dict(eop=usb_ls.EOP_ACCEPTED)),
        (usb_packet(DATA1, some_bytes(source, 3)), dict(skew=usb_ls.TRANSITION_SE0)),
        (usb_packet(DATA0, STUFFED_AT_END), {}),
    )
    events, ch1, ch2, t = [], b"", b"", some_phase(source)
    for n, (packet, how) in enumerate(packets):
        more, back_to_j = usb_ls.send(USB_PINS, packet, t, **how)
        events += more
        # The first four come at random phases, and the rest as close as the specification allows.
        t = back_to_j + 2 * usb_ls.BIT + (3 * US + source.uniform(0, CLK) if n < 4 else 1)
        ch1 += packet
        ch2 += bytes([1 if n == 3 else 0, len(packet)])
    player = chip.board.play(events)
    await chip.expect(ch1=ch1, ch2=ch2, within=1000 * US)
    await player
    await chip.finish()


# ---- 10 Mbit Ethernet


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_scenario_eth10_tx(dut):
    """13. eth10_tx: frames of 60 and 300 bytes, the CRC-32 check string and a full 1514-byte frame, as close
    as the interframe gap allows, fed by the host in real time."""
    source = rng("eth tx")
    frames = [some_bytes(source, 60), some_bytes(source, 300), crc.CHECK_INPUT, some_bytes(source, 1514)]
    chip = await boot(dut, "eth10_tx", outputs=[P0, P1], pulled_low=[P0, P1])
    chip.send(b"".join(len(frame).to_bytes(2, "little") + frame for frame in frames))
    await chip.sent(3000 * US)
    await chip.quiet([P0, P1], 15 * US, 100 * US)
    board = chip.board
    got = eth10.decode((board.line[P0], board.line[P1]), board.since)
    assert len(got) == len(frames), "%d frames on the wire, not %d" % (len(got), len(frames))
    for sent, frame in zip(got, frames):
        assert sent[:-4] == frame, "a %d-byte frame went out as %s..." % (len(frame), sent[:24].hex())
        assert sent[-4:] == zlib.crc32(frame).to_bytes(4, "little"), "FCS %s is wrong" % sent[-4:].hex()
    assert got[2][-4:] == bytes.fromhex("2639f4cb"), "not the catalogue's check value, 0xCBF43926"
    assert board.chip[P0].at(now()) == board.chip[P1].at(now()) == 0, "the idle pair is not driven low"
    await chip.finish()


@cocotb.test(timeout_time=5, timeout_unit="ms")
async def test_scenario_eth10_rx(dut):
    """14. eth10_rx: frames of 64, 65 and 300 bytes and a full 1518, 9.6 us apart, 200 ppm off either way,
    with a transmitter's jitter, read by the host in real time. The third has a flipped bit."""
    source = rng("eth rx")
    chip = await boot(dut, "eth10_rx", pulled_low=[P2])

    def wander(n):
        return source.uniform(-eth10.TRANSMIT_JITTER, eth10.TRANSMIT_JITTER)

    events, ch1, ch2, t = [], b"", b"", some_phase(source)
    worst = 2 * eth10.PPM  # the two stations' clocks, each at its limit
    for n, (size, ppm) in enumerate(((64, worst), (65, -worst), (300, 37), (1518, -worst))):
        data = some_bytes(source, size - 4)
        frame = bytearray(data + zlib.crc32(data).to_bytes(4, "little"))
        if n == 2:
            frame[100] ^= 0x08
        more, end = eth10.send(P2, bytes(frame), t, ppm, wander)
        events += more
        t = end + eth10.IFG + source.uniform(0, CLK)
        ch1 += frame
        ch2 += bytes([1 if n == 2 else 0]) + size.to_bytes(2, "little")
    player = chip.board.play(events)
    await chip.expect(ch1=ch1, ch2=ch2, within=3000 * US)
    await player
    await chip.finish()
