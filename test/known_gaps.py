# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Known gaps between the shipped programs and their protocols, each as the smallest run that shows it.

    make -B COCOTB_TEST_MODULES=known_gaps

Every test here states what the protocol or the program's own interface asks for, so each one FAILS until
its gap is closed, and a chip that does nothing at all passes none of them. They are not in the default run
and weaken nothing there: when one passes, move it into test_programs.py, body unchanged.
"""

import zlib

import cocotb

from bench import boot, now
from host_link import ST_OVERFLOW
from protocols import crc, eth10, i2c, usb_ls
from protocols.wave import CLK, EPS, US

P0, P1, P2 = 0, 1, 2
USB_PINS = DP, DM = P0, P1
DATA0, ACK = usb_ls.DATA0, usb_ls.ACK


@cocotb.test(timeout_time=3, timeout_unit="ms")
async def gap_i2c_plain_read(dut):
    """i2c_ctrl, [addr][0][n]: a read is START, address+R. It first sends address+W, which not every target takes.
    Scenario 7's wording and test_scenario_i2c_ctrl ask for that address+W today, so this is the one gap that
    can't close beside the default run until that request's meaning is changed."""
    chip = await boot(dut, "i2c_ctrl", open_drain=[P0, P1])
    i2c.Target(chip.board, P0, P1, {0x50: b"\x11"}, i2c.FAST, 0)
    chip.send(bytes([0x50, 0, 1]))
    await chip.expect(ch1=b"\x11", ch2=b"\x00", within=300 * US)
    await chip.quiet([P0, P1], 10 * US, 50 * US)
    got, _ = i2c.check(chip.board, P0, P1, i2c.FAST, chip.board.since)
    assert got == ["S A1+ 11- P"], "the bus carried %s" % got
    await chip.finish()


@cocotb.test(timeout_time=1, timeout_unit="ms")
async def gap_eth10_tx_underrun(dut):
    """eth10_tx: the host stops after 30 of 60 bytes. A cut-short frame must go out broken or flagged. It goes
    out padded with 0s under a valid FCS, and the late bytes would be read as the next frame's length."""
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
    drops the link. A pulse is 60 to 200 ns of positive level. About 1.5 million clocks, so half a minute."""
    chip = await boot(dut, "eth10_tx", outputs=[P0, P1], pulled_low=[P0, P1], poll=1000 * US)
    start = now()
    await chip.wait(eth10.LINK_PERIOD[1] + 100 * US)
    tp, tn = chip.board.line[P0], chip.board.line[P1]
    edges = tp.changes(start + CLK)
    assert edges, "no link pulse in 24 ms"
    assert len(edges) >= 2 and edges[0][1] == 1, "TX+ moved, but not as a pulse"
    width = edges[1][0] - edges[0][0]
    assert eth10.LINK_PULSE[0] - EPS <= width <= eth10.LINK_PULSE[1] + EPS, "a link pulse of %.1f ns" % width
    assert not tn.changes(start + CLK) and tn.at(now()) == 0, "TX- moved with the link pulse"
