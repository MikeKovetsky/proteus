# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""I2C from NXP's UM10204: the byte format, START and STOP, clock stretching, and the timing table.

Where a limit is measured depends on the edge. A low time and a setup time before a rise end when the line is
released (the start of its rise). A high time and a setup time after a rise begin when the line has reached
high. So with a rise time the checks are the strict side of the table's 30% and 70% levels.
"""

from collections import namedtuple

from .wave import EPS, US, bytes_msb, change_times

Mode = namedtuple("Mode", "name fmax hd_sta low high su_sta su_dat vd_dat su_sto buf tr_max")
STANDARD = Mode("standard", 100e3, 4.0 * US, 4.7 * US, 4.0 * US, 4.7 * US, 250, 3.45 * US, 4.0 * US, 4.7 * US, 1000)
FAST = Mode("fast", 400e3, 0.6 * US, 1.3 * US, 0.6 * US, 0.6 * US, 100, 0.9 * US, 0.6 * US, 1.3 * US, 300)


class Target:
    """The targets at the addresses of `reads`, each answering reads with its bytes. Data and ACK change as
    late after SCL falls as tVD allows. `stretch` maps the count of ninth clocks so far to how long SCL is
    then held low. `refuse` holds the counts of ninth clocks whose ACK is withheld."""

    def __init__(self, board, scl, sda, reads, mode, rise, stretch=None, refuse=()):
        self.board, self.scl, self.sda = board, scl, sda
        self.reads = {addr: list(data) for addr, data in reads.items()}
        self.delay = mode.vd_dat - rise - 10
        self.stretch, self.refuse = dict(stretch or {}), set(refuse)
        self.acks = 0  # ninth clocks seen
        self.role, self.bits, self.pending, self.token = None, [], None, 0
        self.out, self.addr = [], None
        self.acked = False
        board.listen(self.on_change)

    def set_sda(self, level):
        self.token += 1
        token = self.token
        self.board.later(self.delay, lambda: token == self.token and self.board.drive({self.sda: level}))

    def on_change(self, t, changed):
        if self.sda in changed and self.scl not in changed and self.board.level(self.scl):
            self.token += 1
            self.bits, self.pending = [], None
            self.role = "address" if changed[self.sda] == 0 else None
            self.board.drive({self.sda: None})
        elif self.scl in changed:
            if changed[self.scl]:
                self.pending = self.board.level(self.sda)
            elif self.pending is not None:
                self.bits.append(self.pending)
                self.pending = None
                self.after_fall()

    def after_fall(self):
        if len(self.bits) == 8:  # the ACK bit comes next
            self.acked = False
            if self.role == "address":
                byte = bytes_msb(self.bits)[0]
                self.addr, self.acked = byte >> 1, byte >> 1 in self.reads
            elif self.role == "write":
                self.acked = True
            self.acked &= self.acks + 1 not in self.refuse
            self.set_sda(0 if self.acked else None)
        elif len(self.bits) == 9:
            self.acks += 1
            byte = bytes_msb(self.bits[:8])[0]
            if self.role == "address":
                self.role = (("read" if byte & 1 else "write") if self.acked else None)
            elif self.role == "write" and not self.acked:
                self.role = None
            elif self.role == "read" and self.bits[8]:
                self.role = None  # the controller's NACK: the last byte
            self.bits = []
            if self.role == "read":
                self.out = [self.reads[self.addr].pop(0) if self.reads[self.addr] else 0xFF]
                self.set_sda(None if self.out[0] & 0x80 else 0)
            else:
                self.set_sda(None)
            if self.acks in self.stretch:
                self.board.drive({self.scl: 0})
                self.board.later(self.stretch[self.acks], lambda: self.board.drive({self.scl: None}))
        elif self.role == "read":
            self.set_sda(None if self.out[0] >> 7 - len(self.bits) & 1 else 0)


def check(board, scl, sda, mode, since):
    """Decode the bus and hold every transition to the mode's table. Returns the transactions as text
    ("S A0+ 10+ Sr A1+ 55- P", + for ACK) and the SCL periods that nobody stretched."""
    c, d = board.line[scl], board.line[sda]
    c_wire, d_wire = board.wire[scl], board.wire[sda]
    assert c.at(since) == 1 and d.at(since) == 1, "I2C: the bus is not idle high at the start"

    def least(value, limit, what, t):
        assert value >= limit - EPS, (
            "I2C %s-mode: %s is %.1f ns at %.0f ns, the minimum is %.1f" % (mode.name, what, value, t, limit))

    transactions, periods = [], []
    words = None  # the transaction being read
    bits, pending = [], None
    fall = rise = start = stop = previous_fall = None
    in_low = []  # changes of SDA in this low time
    for t in change_times((c, d), since):
        c_changed = bool(c.changes(t - EPS / 10, t + EPS / 10))
        d_changed = bool(d.changes(t - EPS / 10, t + EPS / 10))
        assert not (c_changed and d_changed), "I2C: SDA and SCL change together at %.0f ns" % t
        if d_changed and c.at(t) == 0:
            assert words is not None, "I2C: SDA moves on an idle bus with SCL low"
            in_low.append(t)
        elif d_changed and d.at(t) == 0:  # START
            assert not bits, "I2C: a START inside a byte at %.0f ns" % t
            if words is None:
                words = ["S"]
                if stop is not None:
                    least(t - stop, mode.buf, "tBUF", t)
            else:
                assert rise is not None, "I2C: a second START with no clock between"
                least(t - rise, mode.su_sta, "tSU;STA", t)
                words.append("Sr")
            start, pending, previous_fall, rise = t, None, None, None
        elif d_changed:  # STOP
            assert words is not None and rise is not None, "I2C: a STOP with no transaction at %.0f ns" % t
            assert not bits, "I2C: a STOP inside a byte at %.0f ns" % t
            least(d_wire.last_change(t, 1) - rise, mode.su_sto, "tSU;STO", t)
            transactions.append(" ".join(words + ["P"]))
            words, pending, stop, rise = None, None, t, None
        elif c.at(t):  # SCL rises
            assert words is not None and fall is not None, "I2C: a clock outside a transaction"
            released = c_wire.last_change(t, 1)
            low = released - fall
            least(low, mode.low, "tLOW", t)
            for change in in_low:
                assert change > fall, "I2C: no data hold"
                # The table's maximum only binds a device that does not lengthen the low time (its note 4).
                latest = mode.vd_dat + max(0.0, low - mode.low)
                assert change - fall <= latest + EPS, "I2C %s-mode: tVD;DAT is %.1f ns" % (mode.name, change - fall)
            if in_low:
                least(released - in_low[-1], mode.su_dat, "tSU;DAT", t)
            rise, pending, in_low = t, d.at(t), []
        else:  # SCL falls
            assert words is not None, "I2C: a clock with no START at %.0f ns" % t
            if rise is None:
                least(t - start, mode.hd_sta, "tHD;STA", t)
            else:
                least(t - rise, mode.high, "tHIGH", t)
                if previous_fall is not None:
                    least(t - previous_fall, 1e9 / mode.fmax, "the SCL period", t)
                    far = board.far[scl]
                    if far.at(previous_fall) != 0 and not far.changes(previous_fall, t):
                        periods.append(t - previous_fall)
                bits.append(pending)
                if len(bits) == 9:
                    words.append("%02X%s" % (bytes_msb(bits[:8])[0], "-" if bits[8] else "+"))
                    bits = []
            fall = previous_fall = t
            rise = pending = None
            in_low = []
    assert words is None, "I2C: the last transaction has no STOP"
    return transactions, periods
