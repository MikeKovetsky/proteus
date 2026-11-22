# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""1-Wire at standard speed, from the AC electrical characteristics of Maxim's DS18B20 and DS2401 data sheets
(test/README.md, "Where the limits come from"). Both tables give every figure below, except that only the
DS2401's caps the reset pulse, at 960 us. The DS18B20's note allows more unless it runs on parasite power.
"""

from .wave import EPS, US, bits_lsb, bytes_lsb

RSTL = (480 * US, 960 * US)  # tRSTL
RSTH = 480 * US  # tRSTH
PDH = (15 * US, 60 * US)  # tPDHIGH
PDL = (60 * US, 240 * US)  # tPDLOW
SLOT = 60 * US  # tSLOT
REC = 1 * US  # tREC
LOW0 = (60 * US, 120 * US)  # tLOW0
LOW1 = (1 * US, 15 * US)  # tLOW1, also the controller's low in a read slot
RDV = 15 * US  # tRDV: a device's bit is valid only this long from the slot's start
DEVICE_SAMPLE = 30 * US  # the test device's own choice, inside the 15 to 60 us a device may sample in


class Device:
    """A device that answers Read ROM (0x33). It gives presence after `pdh` for `pdl`, and sends a 0 by holding
    the line low for `zero_low` from the slot's start."""

    def __init__(self, board, pin, rom, pdh, pdl, zero_low):
        self.board, self.pin, self.rom = board, pin, bits_lsb(rom)
        self.pdh, self.pdl, self.zero_low = pdh, pdl, zero_low
        self.command, self.present, self.holding = [], False, False
        self.fell = None
        board.listen(self.on_change)

    def hold(self, time):
        self.holding = True
        self.board.drive({self.pin: 0})
        self.board.later(time, self.release)

    def release(self):
        self.board.drive({self.pin: None})
        self.holding = False

    def on_change(self, t, changed):
        if self.pin not in changed:
            return
        if changed[self.pin]:
            if self.fell is not None and t - self.fell >= RSTL[0]:
                self.command = []
                self.present = True
                self.board.later(self.pdh, lambda: self.hold(self.pdl))
            self.fell = None
        elif not self.holding:
            self.fell = t
            if not self.present:
                return
            if len(self.command) < 8:
                self.board.later(DEVICE_SAMPLE, lambda: self.command.append(self.board.level(self.pin)))
            elif bytes_lsb(self.command) == b"\x33" and self.rom:
                if self.rom.pop(0) == 0:
                    self.hold(self.zero_low)


def check_controller(line, driven, since, read_bits):
    """Hold the chip's low pulses to the table and return the bytes it wrote. `read_bits` read slots follow
    the 8 of the command; with none, the reset pulse is all there is."""
    lows = []
    for t, level in driven.changes(since):
        if level == 0:
            lows.append([t])
        elif lows and len(lows[-1]) == 1:
            lows[-1].append(t)
    assert lows, "1-Wire: no reset pulse"
    assert all(len(low) == 2 for low in lows), "1-Wire: the chip left the line low"

    reset_fall, reset_rise = lows[0]
    width = reset_rise - reset_fall
    assert RSTL[0] - EPS <= width <= RSTL[1] + EPS, "1-Wire: the reset pulse is %.1f us" % (width / US)
    slots = lows[1:]
    assert len(slots) == (8 + read_bits if read_bits else 0), "1-Wire: %d time slots" % len(slots)
    if not slots:
        return b""
    assert slots[0][0] - reset_rise >= RSTH - EPS, "1-Wire: tRSTH is %.1f us" % ((slots[0][0] - reset_rise) / US)

    written = []
    for n, (fall, rise) in enumerate(slots):
        width = rise - fall
        if n < 8 and LOW0[0] - EPS <= width <= LOW0[1] + EPS:
            written.append(0)
        else:
            assert LOW1[0] - EPS <= width < LOW1[1], "1-Wire: slot %d is low for %.1f us" % (n, width / US)
            if n < 8:
                written.append(1)
        if n < 8:  # a device samples anywhere from 15 to 60 us
            assert not line.changes(fall + LOW1[1], fall + LOW0[0]) and line.at(fall + LOW1[1]) == written[-1], (
                "1-Wire: write slot %d is not steady from 15 to 60 us" % n)
        if n + 1 < len(slots):
            following = slots[n + 1][0]
            assert following - fall >= SLOT + REC - EPS, "1-Wire: slots %.1f us apart" % ((following - fall) / US)
            before = following - 2 * EPS  # the line must have been high for the whole recovery time
            assert line.at(before) == 1 and before - line.last_change(before, 1) >= REC - 3 * EPS, (
                "1-Wire: under 1 us of recovery after slot %d" % n)
    return bytes_lsb(written)
