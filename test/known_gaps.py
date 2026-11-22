# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Known gaps between the shipped programs and their protocols, each as the smallest run that shows it.

    make -B COCOTB_TEST_MODULES=known_gaps

Every test here states what the protocol or the program's own interface asks for, so each one FAILS until
its gap is closed. They are not in the default run and weaken nothing there: when one passes, move it into
test_scenarios.py.
"""

import zlib

import cocotb

from bench import boot, now
from host_link import ST_OVERFLOW
from protocols import crc, eth10, i2c, ps2, spi, uart, usb_ls, ws2812
from protocols.wave import CLK, US, bits_lsb

P0, P1, P2, P3 = 0, 1, 2, 3
SPI_PINS = SCK, MOSI, MISO, CS = P0, P1, P2, P3
USB_PINS = DP, DM = P0, P1
DATA0, ACK = 0xC3, 0xD2


@cocotb.test(timeout_time=3, timeout_unit="ms")
async def gap_i2c_write_only(dut):
    """i2c_ctrl, [addr][1][byte][0]: a write with nothing to read must end. N wraps and it reads 65,536 bytes."""
    chip = await boot(dut, "i2c_ctrl", open_drain=[P0, P1])
    i2c.Target(chip.board, P0, P1, {0x50: b"\x11\x22"}, i2c.FAST, 0)
    chip.send(bytes([0x50, 1, 0x77, 0]))
    await chip.expect(ch2=b"\x00", within=300 * US)
    await chip.finish()


@cocotb.test(timeout_time=3, timeout_unit="ms")
async def gap_i2c_plain_read(dut):
    """i2c_ctrl, [addr][0][n]: a read is START, address+R. It first sends address+W, which not every target takes."""
    chip = await boot(dut, "i2c_ctrl", open_drain=[P0, P1])
    i2c.Target(chip.board, P0, P1, {0x50: b"\x11"}, i2c.FAST, 0)
    chip.send(bytes([0x50, 0, 1]))
    await chip.expect(ch1=b"\x11", ch2=b"\x00", within=300 * US)
    await chip.quiet([P0, P1], 10 * US, 50 * US)
    got, _ = i2c.check(chip.board, P0, P1, i2c.FAST, chip.board.since)
    assert got == ["S A1+ 11- P"], "the bus carried %s" % got


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def gap_spi_target_keeps_queued_byte(dut):
    """spi_target_m0: two bytes queued, two 1-byte transactions. The second byte is popped and lost as CS rises."""
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


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def gap_spi_ctrl_samples_early(dut):
    """spi_ctrl_m0: a target whose data is valid 20 ns before the rising edge of a 5 MHz clock is in order.
    The controller samples the pad 3 clocks (50 ns) before that edge."""
    chip = await boot(dut, "spi_ctrl_m0", outputs=[SCK, MOSI, CS], pulled_low=[SCK])
    spi.Target(chip.board, SPI_PINS, 0, [(b"\xa5", spi.PERIOD / 2 - 20)])
    chip.send(b"\x01\x00")
    await chip.expect(ch1=b"\xa5", within=20 * US)


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def gap_underrun_is_silent(dut):
    """spi_ctrl_m0 (eth10_tx and usb_ls_tx alike): the host is late with byte 2. The chip must wait or flag
    it. It pops the empty FIFO and sends a 0, and OVERFLOW stays low."""
    chip = await boot(dut, "spi_ctrl_m0", outputs=[SCK, MOSI, CS], pulled_low=[SCK])
    chip.send(b"\x02\xff")
    await chip.wait(20 * US)
    rises = sum(1 for _, level in chip.board.line[SCK].changes(chip.board.since) if level)
    assert rises <= 8 or chip.host.status() & ST_OVERFLOW, "%d clocks for 1 byte from the host, and no flag" % rises


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def gap_eth10_tx_underrun(dut):
    """eth10_tx: the host stops after 30 of 60 bytes. A cut-short frame must go out broken or flagged. It goes
    out padded with 0s under a valid FCS, and the late bytes would be read as the next frame's length."""
    chip = await boot(dut, "eth10_tx", outputs=[P0, P1], pulled_low=[P0, P1])
    chip.send(b"\x3c\x00" + bytes(range(1, 31)))
    await chip.wait(100 * US)
    if chip.host.status() & ST_OVERFLOW:
        return  # flagged
    try:
        frames = eth10.decode((chip.board.line[P0], chip.board.line[P1]), chip.board.since)
    except AssertionError:
        return  # broken on the wire, which every receiver drops
    for sent in frames:
        valid = sent[-4:] == zlib.crc32(sent[:-4]).to_bytes(4, "little")
        assert not valid, "30 bytes became a valid frame: %s" % sent.hex()


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def gap_uart_rx_framing(dut):
    """uart_rx: a frame whose stop bit is 0 is not a byte."""
    chip = await boot(dut, "uart_rx")
    events, end = uart.send(P1, b"\x42", 115200, now() + 1 * US)
    events = [(t, levels) for t, levels in events if t < end - 1e9 / 115200 - 1]
    events += [(end - 1e9 / 115200, {P1: 0}), (end + 10 * US, {P1: 1})]
    await chip.board.play(events)
    await chip.finish()


@cocotb.test(timeout_time=2, timeout_unit="ms")
async def gap_ps2_rx_framing(dut):
    """ps2_rx: a frame whose stop bit is 0 is not a good byte."""
    chip = await boot(dut, "ps2_rx")
    events, _ = ps2.frame(P0, P1, 0x42, now() + 1 * US, 16.7, late=False, stop=0)
    await chip.board.play(events)
    await chip.wait(10 * US)
    assert not chip.task.received[1], "a frame with a bad stop bit gave %s" % chip.task.received[1].hex()


@cocotb.test(timeout_time=6, timeout_unit="ms")
async def gap_ps2_rx_resync(dut):
    """ps2_rx: a device abandons a frame after 4 clocks. 2 ms later, a whole frame must be read."""
    chip = await boot(dut, "ps2_rx")
    start = now() + 1 * US
    cut, _ = ps2.frame(P0, P1, 0x00, start, 16.7, late=False)
    cut = cut[:4 * 3] + [(start + 300 * US, {P1: None})]
    again, _ = ps2.frame(P0, P1, 0x5A, start + 2000 * US, 16.7, late=False)
    await chip.board.play(cut + again)
    await chip.expect(ch1=b"\x5a", within=100 * US)


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def gap_ws2812_v5(dut):
    """ws2812_tx against WS2812B V5: a 0 is high 380 ns at most, and 280 us low latches."""
    chip = await boot(dut, "ws2812_tx", outputs=[P0], pulled_low=[P0])
    chip.send(b"\x01\x0f\xf0\x55")
    await chip.wait(350 * US)
    ws2812.decode(chip.board.line[P0], chip.board.since, now(), ws2812.WS2812B_V5)


@cocotb.test(timeout_time=10, timeout_unit="ms")
async def gap_onewire_shorted_bus(dut):
    """onewire: a line shorted to ground is no device. It reports present with a good CRC (all zeros have one)."""
    chip = await boot(dut, "onewire", open_drain=[P0], far_end=lambda board: board.drive({P0: 0}), poll=20 * US)
    await chip.until(lambda: len(chip.task.received[2]) >= 1, "status", 7000 * US)
    await chip.wait(50 * US)
    assert chip.task.received[2][:1] == b"\x01", "a shorted bus gave %s" % chip.state()


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def gap_usb_tx_handshake(dut):
    """usb_ls_tx: a device must send ACK, which is SYNC, PID, EOP. Every packet gets a CRC-16."""
    chip = await boot(dut, "usb_ls_tx", outputs=USB_PINS, pulled_low=[DP])
    chip.send(bytes([1, ACK]))
    await chip.quiet(USB_PINS, 30 * US, 100 * US)
    board = chip.board
    got = usb_ls.decode((board.line[DP], board.line[DM]), (board.chip[DP], board.chip[DM]), board.since)
    assert got == [bytes([ACK])], "ACK went out as %s" % [g.hex() for g in got]


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def gap_usb_rx_handshake(dut):
    """usb_ls_rx: an ACK has no CRC, so it cannot have a bad one."""
    chip = await boot(dut, "usb_ls_rx", pulled_low=[DP])
    events, _ = usb_ls.send(USB_PINS, bytes([ACK]), now() + 1 * US)
    await chip.board.play(events)
    await chip.expect(ch1=bytes([ACK]), ch2=b"\x00\x01", within=20 * US)


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def gap_usb_rx_dribble(dut):
    """usb_ls_rx: hubs can stretch the last bit before EOP by up to a bit, and a receiver must accept it
    (7.1.9.1). This one reports a bad CRC from a stretch of just over half a bit."""
    chip = await boot(dut, "usb_ls_rx", pulled_low=[DP])
    packet = bytes([DATA0]) + crc.with_crc(crc.CRC16_USB, b"\x12\x34")
    events, _ = usb_ls.send(USB_PINS, packet, now() + 1 * US)
    events = events[:-3] + [(t + usb_ls.BIT, levels) for t, levels in events[-3:]]
    await chip.board.play(events)
    await chip.expect(ch1=packet, ch2=bytes([0, len(packet)]), within=20 * US)


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def gap_usb_rx_stuff_error(dut):
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
    await chip.board.play(events)
    await chip.until(lambda: len(chip.task.received[2]) >= 2, "status", 20 * US)
    assert chip.task.received[2][0] != 0, "a stuffing error was reported as a good packet"


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def gap_eth10_rx_jitter(dut):
    """eth10_rx: a 10BASE-T receiver takes 13.5 ns of jitter either way on every edge (802.3 clause 14). This
    one loses frames from 8.5 ns: half bits and bits 8.3 ns off land on the wrong side of a clock."""
    chip = await boot(dut, "eth10_rx", pulled_low=[P2])
    frame = crc.with_crc(crc.CRC32_ETHERNET, bytes(range(60)))

    def jitter(n):
        return eth10.RECEIVE_JITTER * (1 if n // 2 % 2 else -1)

    events, _ = eth10.send(P2, frame, now() + 1 * US, ppm=2 * eth10.PPM, jitter=jitter)
    await chip.board.play(events)
    await chip.expect(ch1=frame, ch2=bytes([0, 64, 0]), within=20 * US)


@cocotb.test(timeout_time=30, timeout_unit="ms")
async def gap_eth10_link_pulses(dut):
    """eth10_tx: an idle 10BASE-T transmitter sends a link pulse every 16 ms (8 either way), or the far end
    drops the link. About 1.5 million clocks, so half a minute."""
    chip = await boot(dut, "eth10_tx", outputs=[P0, P1], pulled_low=[P0, P1], poll=1000 * US)
    start = now()
    await chip.wait(24100 * US)
    assert chip.board.line[P0].changes(start + CLK), "no link pulse in 24 ms"
