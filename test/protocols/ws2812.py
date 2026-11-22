# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""WS2812B, from Worldsemi's first data sheet: every bit a high then a low, MSB first, and 50 us low latches.

Parts marked V4 and later have tighter highs and need 280 us to latch; known_gaps.py holds the chip to those.
"""

from collections import namedtuple

from .wave import EPS, US, bytes_msb

Part = namedtuple("Part", "name high0 high1 low0 low1 reset")  # each time is (min, max) in ns
WS2812B = Part("WS2812B", (250, 550), (650, 950), (700, 1000), (300, 600), 50 * US)
WS2812B_V5 = Part("WS2812B-V5", (220, 380), (580, 1000), (580, 1000), (580, 1000), 280 * US)


def decode(line, since, until, part=WS2812B):
    """The frames, each ended by a latching low."""
    assert line.at(since) == 0, "WS2812: the line does not idle low"
    edges = line.changes(since, until)
    assert len(edges) % 2 == 0, "WS2812: the line stayed high"
    frames, bits = [], []
    for n in range(0, len(edges), 2):
        up, down = edges[n][0], edges[n + 1][0]
        next_up = edges[n + 2][0] if n + 2 < len(edges) else until
        high, low = down - up, next_up - down
        kinds = [b for b, (lo, hi) in enumerate((part.high0, part.high1)) if lo - EPS <= high <= hi + EPS]
        assert len(kinds) == 1, "%s: a high of %.1f ns is neither a 0 nor a 1" % (part.name, high)
        bits.append(kinds[0])
        if low >= part.reset - EPS:
            frames.append(bytes_msb(bits))
            bits = []
        else:
            lo, hi = part.low1 if kinds[0] else part.low0
            assert lo - EPS <= low <= hi + EPS, "%s: a %d is low for %.1f ns" % (part.name, kinds[0], low)
    assert not bits, "%s: the last frame has no latch" % part.name
    return frames
