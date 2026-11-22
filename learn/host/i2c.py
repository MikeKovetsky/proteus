# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The I2C adapter: a target, seen an event at a time.

Symbols: 0 to 255 write that byte and answer 0 ACK or 1 NACK. 256 + x makes a START or repeated START, sends x
(address and R/W) and answers the same. 512 reads a byte and ACKs it, 513 reads and NACKs it, both answering
the byte. 514 is STOP.
Phases: 0 idle, 1 the target takes bytes, 2 the target sends bytes, 3 refused or ended (only START or STOP)."""

from . import probes
from .bus import Bus

START, ACK, NACK, STOP = 256, 512, 513, 514
IDLE, WRITE, READ, ENDED = 0, 1, 2, 3
DATA, ADDRESS_W, ADDRESS_R = 0, 1, 2
PROBES = 3


class I2c(Bus):
    NAME = "i2c"
    WIRING = "P0 SCL, P1 SDA (both pulled up), P4 nRESET; the device alone on the bus"
    SYMBOLS = 515
    KINDS = 3
    PAUSE = 6
    ENGINES = probes.I2C

    def allows(self, phase, s):
        if phase == READ:
            return s == ACK or s == NACK
        if START <= s < ACK:
            return True
        if phase == WRITE:
            return s < START or s == STOP
        return phase == ENDED and s == STOP

    def after(self, phase, s, out):
        if s == STOP:
            return IDLE
        if s == ACK:
            return READ
        if s == NACK or out != 0:
            return ENDED
        if s < START:
            return WRITE
        return READ if s & 1 else WRITE

    def allowed_count(self, phase):
        return (256, 513, 2, 257)[phase]

    def allowed(self, phase, i):
        if phase == READ:
            return ACK + i
        if phase == WRITE:
            return i if i < 512 else STOP
        return START + i if i < 256 else STOP

    def kind(self, s):
        if s < START:
            return DATA
        if s < ACK:
            return ADDRESS_R if s & 1 else ADDRESS_W
        return None

    def kind_size(self, kind):
        return 256 if kind == DATA else 128

    def member(self, kind, i):
        if kind == DATA:
            return i % 256
        return START + 2 * (i % 128) + (1 if kind == ADDRESS_R else 0)

    def controls(self):
        return [ACK, NACK, STOP]

    def defaults(self):
        address = (0x64 + self.seed * 26) & 0xFE
        return [(0x5B + self.seed * 37) & 0xFF, START + address, START + (address | 1)]

    def operation(self, s):
        if s < START:
            return (2, s)
        if s < ACK:
            return (1, s - START)
        return (3 + s - ACK,)

    def show(self, s):
        if s < START:
            return "%02X" % s
        if s < ACK:
            return "START %02X+%s" % ((s - START) >> 1, "R" if s & 1 else "W")
        return ("read+ACK", "read+NACK", "STOP")[s - ACK]

    def check(self, phase, answers):
        if ACK in answers and NACK in answers and answers[ACK] != answers[NACK]:
            return "a byte read depends on the ACK that follows it"
        for s in answers:
            if (START <= s < ACK or s == STOP or s < START) and answers[s] > 1:
                return "SDA is held low where the bus needs it high"
        return None

    def absent(self, model):
        for row in model.rows:
            for s in row:
                if START <= s < ACK and row[s][1] == 0:
                    return None
        return "no address was acknowledged, as on a bus with no device on it"

    # ---- what the search probes with

    def scouts(self, phase, s):
        if s < START:
            return True
        return s < ACK and phase == IDLE  # what a START does seldom depends on where it falls. The sweep has all

    def found(self, model, kind):
        skip = model.defaults[kind] if model.defaults else None
        return [a for a in model.alphabet if a != skip and self.kind(a) == kind]

    def probes(self, model, turn):
        reads, writes = self.found(model, ADDRESS_R), self.found(model, ADDRESS_W)
        data = self.found(model, DATA)
        word = []
        for i in range(PROBES):
            if writes and data:
                word += [writes[(turn // PROBES + i) % len(writes)], data[(turn + i) % len(data)]]
            if reads:
                word += [reads[(turn // PROBES + i) % len(reads)], ACK, NACK, STOP]
            if not data and i:
                break
        return word

    def witnesses(self, model, phase, s, turn):
        reads, data = self.found(model, ADDRESS_R), self.found(model, DATA)
        if model.defaults:
            data.append(model.defaults[DATA])
        if reads and turn & 1:
            first = [reads[(turn >> 1) % len(reads)], ACK, NACK, STOP]
        elif data:
            first = [data[(turn >> 1) % len(data)], ACK, NACK, STOP]
        else:
            first = [ACK, NACK, STOP]
        return (first + self.probes(model, turn),)

    def probe(self, model, turn):
        return [ACK, NACK, STOP] + self.probes(model, turn)

    def transactions(self, model):
        writes, reads = self.found(model, ADDRESS_W), self.found(model, ADDRESS_R)
        shapes = [(r, ACK, NACK, STOP) for r in reads]
        for w in writes:
            for d in self.found(model, DATA):
                shapes.append((w, d, STOP))
        return shapes

    def clone(self, model):
        from . import clone_i2c
        return clone_i2c.generate(self, model)
