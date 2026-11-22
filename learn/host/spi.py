# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The SPI adapter: a target in mode 0, MSB first, seen a byte at a time.

Symbols: 0 to 255 clock that byte out on MOSI and answer the byte that came back, 256 selects, 257 deselects.
Phases: 0 CS high, 1 CS low. A target must decide the byte it sends before it has the one it receives. The
learner checks that, and refuses a device that does not."""

from . import probes
from .bus import Bus

SELECT, DESELECT = 256, 257
HIGH, LOW = 0, 1
PROBES = 4  # commands probed in one scout test


class Spi(Bus):
    NAME = "spi"
    WIRING = "P0 SCK, P1 MOSI, P2 MISO, P3 CS (low to select), P4 nRESET; mode 0, MSB first"
    SYMBOLS = 258
    KINDS = 1
    PAUSE = 4
    ENGINES = probes.SPI

    def allows(self, phase, s):
        return s == SELECT if phase == HIGH else s != SELECT

    def after(self, phase, s, out):
        if s == SELECT:
            return LOW
        return HIGH if s == DESELECT else phase

    def allowed_count(self, phase):
        return 1 if phase == HIGH else 257

    def allowed(self, phase, i):
        if phase == HIGH:
            return SELECT
        return i if i < 256 else DESELECT

    def kind(self, s):
        return 0 if s < 256 else None

    def kind_size(self, kind):
        return 256

    def member(self, kind, i):
        return i % 256

    def controls(self):
        return [SELECT, DESELECT]

    def defaults(self):
        return [(0x5B + self.seed * 37) & 0xFF]

    def operation(self, s):
        if s == SELECT:
            return (1,)
        if s == DESELECT:
            return (2,)
        return (3, s)

    def show(self, s):
        if s == SELECT:
            return "select"
        return "deselect" if s == DESELECT else "%02X" % s

    def check(self, phase, answers):
        seen = None
        for s in answers:
            if s < 256:
                if seen is not None and answers[s] != seen:
                    return ("two bytes got different answers in one state: MISO depends on the byte still coming "
                            "in, or on what came before the reset. No byte-level state table can copy that")
                seen = answers[s]
        return None

    def absent(self, model):
        seen = []
        for row in model.rows:
            for s in row:
                if s < 256 and row[s][1] not in seen:
                    seen.append(row[s][1])
        if not seen or seen == [0x00] or seen == [0xFF]:
            return "every byte read was all zeros or all ones, as from a bus with no device on it"
        return None

    # ---- what the search probes with

    def scouts(self, phase, s):
        return s < 256

    def commands(self, model):
        return [a for a in model.alphabet if a < 256 and (not model.defaults or a != model.defaults[0])]

    def probes(self, model, turn):
        found, word = self.commands(model), []
        filler = model.defaults[0] if model.defaults else 0
        for i in range(min(PROBES, len(found))):
            word += [SELECT, found[(turn + i) % len(found)], filler, DESELECT]
        return word

    def witnesses(self, model, phase, s, turn):
        filler = model.defaults[0] if model.defaults else 0
        follows = self.commands(model) + [filler]
        follow = follows[turn % len(follows)]
        # s with data after it, and s alone: each followed by what would read the effect back
        return ([follow, filler, DESELECT] + self.probes(model, turn) + [SELECT, filler, DESELECT],
                [DESELECT] + self.probes(model, turn) + [SELECT, s, filler, DESELECT])

    def probe(self, model, turn):
        filler = model.defaults[0] if model.defaults else 0
        return [filler, DESELECT] + self.probes(model, turn) + [SELECT, filler]

    def transactions(self, model):
        return [(SELECT, a, DESELECT) for a in self.commands(model)]

    def clone(self, model):
        from . import clone_spi
        return clone_spi.generate(self, model)
