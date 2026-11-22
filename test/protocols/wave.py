# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""A waveform as a list of changes, and the few bit helpers every protocol needs."""

from bisect import bisect_left, bisect_right

CLK = 1000 / 60  # one chip clock at 60 MHz, in ns
US = 1000.0
EPS = 0.01  # ns: the simulator counts whole picoseconds


class Wave:
    """One signal: its level from each time on. A level is 0, 1, or None for "not driven"."""

    def __init__(self, t, level):
        self.times, self.levels = [t], [level]

    def set(self, t, level):
        if t == self.times[-1] and len(self.times) > 1:  # a second change at one instant replaces the first
            self.times.pop()
            self.levels.pop()
        if level != self.levels[-1]:
            self.times.append(t)
            self.levels.append(level)

    def at(self, t):
        return self.levels[max(bisect_right(self.times, t) - 1, 0)]

    def changes(self, start, end=float("inf")):
        """[(t, new level)] with start < t < end."""
        first, last = max(bisect_right(self.times, start), 1), bisect_left(self.times, end)
        return list(zip(self.times[first:last], self.levels[first:last]))

    def last_change(self, t, level):
        """The last time at or before t at which the wave went to `level`."""
        i = bisect_right(self.times, t) - 1
        while i > 0 and self.levels[i] != level:
            i -= 1
        return self.times[i]


def change_times(waves, start, end=float("inf")):
    return sorted({t for wave in waves for t, _ in wave.changes(start, end)})


def bits_lsb(data):
    return [byte >> n & 1 for byte in data for n in range(8)]


def bits_msb(data):
    return [byte >> n & 1 for byte in data for n in range(7, -1, -1)]


def bytes_lsb(bits):
    assert len(bits) % 8 == 0, "%d bits is not a whole number of bytes" % len(bits)
    return bytes(sum(b << n for n, b in enumerate(bits[i:i + 8])) for i in range(0, len(bits), 8))


def bytes_msb(bits):
    assert len(bits) % 8 == 0, "%d bits is not a whole number of bytes" % len(bits)
    return bytes(sum(b << 7 - n for n, b in enumerate(bits[i:i + 8])) for i in range(0, len(bits), 8))
