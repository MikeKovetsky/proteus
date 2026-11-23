# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""What a bus adapter provides. An adapter turns symbols into query-engine operations, and knows the grammar of
its bus. It knows nothing of any device."""

PAUSE_US = 25  # one unit of the query engines' pause


class Refused(Exception):
    """The host will not write a program. The text says why."""

    def __init__(self, code, text):
        super().__init__(text)  # MicroPython has no Exception.__init__(self) to call
        self.code = code
        self.text = text


class Bus:
    """Symbols are small integers. A phase says which symbols the bus allows next, and follows from the symbols
    and answers so far. A kind is a range of symbols (every data byte, say) that share one default."""

    KINDS = 0
    PAUSE = 0  # the engine's pause operation
    ENGINES = {}

    def __init__(self, told):
        self.inputs = None  # or the only symbols the host is told to learn
        if told["inputs"] is not None:
            self.inputs = sorted(set(told["inputs"]))
            for s in self.inputs:
                if not isinstance(s, int) or not 0 <= s < self.SYMBOLS:
                    raise Refused("told", "%r is not a symbol of this bus" % (s,))
        self.rate = told["rate"]
        self.serve = told["serve"] or told["rate"]
        self.reset_us = told["reset_us"]
        self.boot_us = told["boot_us"]
        self.seed = told["seed"]
        if self.rate not in self.ENGINES:
            raise Refused("told", "there is a query engine for %s kHz only" % sorted(self.ENGINES))

    def openers(self, model):
        """The first symbols of the pairs stage's openings: none, unless the bus has openings to try."""
        return []

    def image(self):
        return self.ENGINES[self.rate]

    def enabled(self, phase, s):
        return self.allows(phase, s) and (self.inputs is None or s in self.inputs)

    def count(self, phase):
        if self.inputs is None:
            return self.allowed_count(phase)
        return len([s for s in self.inputs if self.allows(phase, s)])

    def nth(self, phase, i):
        if self.inputs is None:
            return self.allowed(phase, i)
        return [s for s in self.inputs if self.allows(phase, s)][i]

    def alphabet(self):
        """The symbols learned in full from the start: the bus's own events and one default of each kind."""
        if self.inputs is not None:
            return list(self.inputs), []
        defaults = self.defaults()
        return self.controls() + defaults, list(defaults)

    def encode(self, word, pauses=(), pause_us=0):
        """Reset, then the word, as the bytes the query engine takes on channel 0."""
        out = bytearray((0, self.reset_us, self.boot_us))
        units = (pause_us + PAUSE_US - 1) // PAUSE_US
        for i in range(len(word) + 1):
            if i in pauses:
                left = units
                while left:
                    part = left if left < 255 else 255
                    out.append(self.PAUSE)
                    out.append(part)
                    left -= part
            if i < len(word):
                for byte in self.operation(word[i]):
                    out.append(byte)
        return bytes(out)

    def decode(self, word, raw):
        """The answers, with -1 from the first symbol the bus did not allow at that point."""
        out, phase, legal = [], 0, True
        for i in range(len(word)):
            legal = legal and self.allows(phase, word[i])
            if legal:
                out.append(raw[i])
                phase = self.after(phase, word[i], raw[i])
            else:
                out.append(-1)
        return tuple(out)
