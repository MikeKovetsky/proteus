# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""SPI modes 0 and 3 as Motorola's SPI Block Guide V02.07 defines them, MSB first, CS active low.

Both sample on the rising edge of SCK and change data on the falling edge. Mode 0 idles SCK low and puts the
first bit out as CS falls; mode 3 idles it high and puts the first bit out on the first falling edge. SPI has
no timing standard, so each limit says where it comes from: the block guide, the scenario (bakeoff/README.md,
4 to 6), or the test's own choice. A real part for scale is Microchip's 25LC256 (its AC characteristics).
"""

from .wave import CLK, EPS, bits_msb, bytes_msb

IDLE = {0: 0, 3: 1}  # SCK while CS changes
PERIOD = 200.0  # the scenario's 5 MHz
PERIOD_TOLERANCE = CLK  # the scenario's
CS_SETUP = 6 * CLK  # CS low to the first edge: the scenario's, and the block guide's tL of half a period
CS_HOLD = PERIOD / 2  # the last edge of SCK to CS high: the block guide's tT, at least half a period
CS_HIGH = PERIOD / 2  # CS high between transactions: the block guide's tI, at least half a period
DATA_MARGIN = PERIOD / 2 - CLK  # MOSI round the rising edge, and each phase of SCK: the test's own, stricter
                                # than a 25LC256 at 5 V asks (10 ns setup, 20 ns hold, 50 ns a phase)
TARGET_VALID = 50.0  # how late the test's slower target answers: a 25LC256's tV at 4.5 to 5.5 V
TARGET_PERIOD = 500.0  # the scenario's 2 MHz
TARGET_LEAD = 60 * CLK  # CS low to the first rising edge: the scenario's
TARGET_SETUP = 3 * CLK  # MISO before the rising edge: the scenario's. It holds to the falling edge, where mode 0
                        # shifts, which is stricter than the scenario's one clock
TARGET_DISABLE = 100.0  # CS high to MISO released: the test's own (a 25LC256 takes 40 to 160 ns by supply)


class Target:
    """A target on the bus with one (reply, valid after) pair per transaction. A bit is valid that many ns
    after the edge that launches it. Until then MISO carries the opposite level, so a controller that samples
    inside that window reads wrong data."""

    def __init__(self, board, pins, mode, replies):
        self.board, self.mode, self.valid_after = board, mode, 0
        self.sck, self.mosi, self.miso, self.cs = pins
        self.replies = list(replies)
        self.received, self.bits, self.reply = [], None, []
        self.token = 0
        board.listen(self.on_change)

    def put_next_bit(self):
        if not self.reply:
            return
        bit = self.reply.pop(0)
        self.token += 1
        token = self.token
        if self.valid_after:
            self.board.drive({self.miso: 1 - bit})
            self.board.later(self.valid_after,
                             lambda: token == self.token and self.board.drive({self.miso: bit}))
        else:
            self.board.drive({self.miso: bit})

    def on_change(self, t, changed):
        if changed.get(self.cs) == 1 and self.bits is not None:
            self.received.append(bytes_msb(self.bits))
            self.bits = None
            self.token += 1
            self.board.drive({self.miso: None})
        elif changed.get(self.cs) == 0:
            self.bits = []
            reply, self.valid_after = self.replies.pop(0) if self.replies else (b"", 0)
            self.reply = bits_msb(reply)
            if self.mode == 0:
                self.put_next_bit()
        elif self.bits is not None and self.sck in changed:
            if changed[self.sck]:
                self.bits.append(self.board.level(self.mosi))
            else:
                self.put_next_bit()


def check_controller(sck, mosi, cs, mode, since):
    """The bytes of every transaction, after checking the clock, CS and MOSI against the mode."""
    assert cs.at(since) == 1, "SPI: CS is not high before the first transaction"
    assert sck.at(since) == IDLE[mode], "SPI: SCK does not idle %d in mode %d" % (IDLE[mode], mode)
    cs_edges = cs.changes(since)
    assert len(cs_edges) % 2 == 0, "SPI: CS stayed low"
    out, high_since = [], since
    for (down, _), (up, _) in zip(cs_edges[::2], cs_edges[1::2]):
        assert not sck.changes(high_since, down), "SPI: SCK moved while CS was high"
        assert high_since == since or down - high_since >= CS_HIGH, "SPI: CS was high for too short a time"
        for t in (down, up):
            assert sck.at(t) == IDLE[mode] and all(at != t for at, _ in sck.changes(t - 1, t + 1)), (
                "SPI: SCK is not idle (%d) as CS changes at %.0f ns" % (IDLE[mode], t))
        edges = sck.changes(down, up)
        rises = [t for t, level in edges if level]
        assert rises and len(rises) % 8 == 0, "SPI: %d clocks in one transaction" % len(rises)
        assert edges[0][0] - down >= CS_SETUP - EPS, "SPI: CS setup is %.1f ns" % (edges[0][0] - down)
        assert up - edges[-1][0] >= CS_HOLD - EPS, "SPI: CS hold is %.1f ns" % (up - edges[-1][0])
        for a, b in zip(rises, rises[1:]):
            assert abs(b - a - PERIOD) <= PERIOD_TOLERANCE + EPS, "SPI: SCK period %.1f ns, not 200" % (b - a)
        for (a, _), (b, _) in zip(edges, edges[1:]):
            assert b - a >= DATA_MARGIN - EPS, "SPI: an SCK phase of %.1f ns" % (b - a)
        for t in rises:
            assert not mosi.changes(t - DATA_MARGIN + EPS, t + DATA_MARGIN - EPS), (
                "SPI: MOSI changes within %.1f ns of the rising edge at %.0f ns" % (DATA_MARGIN, t))
        out.append(bytes_msb([mosi.at(t) for t in rises]))
        high_since = up
    assert not sck.changes(high_since), "SPI: SCK moved after the last transaction"
    return out


def controller_mode0(pins, data, start):
    """The events of a mode-0 controller at 2 MHz, its rising edges and the time CS rises."""
    sck, mosi, _, cs = pins
    half, bits = TARGET_PERIOD / 2, bits_msb(data)
    events = [(start, {cs: 0, sck: 0, mosi: bits[0]})]
    rises = [start + TARGET_LEAD + n * TARGET_PERIOD for n in range(len(bits))]
    for n, t in enumerate(rises):
        events.append((t, {sck: 1}))
        fall = {sck: 0}
        if n + 1 < len(bits):
            fall[mosi] = bits[n + 1]
        events.append((t + half, fall))
    end = rises[-1] + 2 * half
    events.append((end, {cs: 1}))
    return events, rises, end


def check_target(miso, driven, start, rises, end, idle_since):
    """The bytes the target sent. `driven` is what the chip drives: None while it has let MISO go."""
    assert driven.at(idle_since) is None and not driven.changes(idle_since, start), (
        "SPI target: MISO is driven while CS is high, before the transaction")
    bits = []
    for t in rises:
        a, b = t - TARGET_SETUP, t + TARGET_PERIOD / 2
        assert driven.at(a) is not None and driven.at(b) is not None, "SPI target: MISO is not driven"
        assert not miso.changes(a, b) and not any(level is None for _, level in driven.changes(a, b)), (
            "SPI target: MISO changes round the rising edge at %.0f ns" % t)
        bits.append(miso.at(t))
    assert driven.at(end + TARGET_DISABLE) is None, "SPI target: MISO is still driven 100 ns after CS rose"
    return bytes_msb(bits)
