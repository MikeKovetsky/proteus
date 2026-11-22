# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""PS/2 device to host, from IBM's PS/2 Hardware Interface Technical Reference, "Keyboard and Auxiliary Device
Controller (Type 1)", figures 7 and 8 (test/README.md, "Where the limits come from").

The device makes both the clock and the data. Each phase of the clock is 30 to 50 us (T3 and T4), so it runs
at 10 to 16.7 kHz. A frame is a 0, 8 data bits LSB first, odd parity and a 1, each valid on a falling clock
edge. Data moves at least 5 us after the clock rises (T2), and 5 to 25 us before it falls (T1).
"""

from .wave import US, bits_lsb

MARGIN = 5 * US  # T2's minimum and T1's
T1_MAX = 25 * US
BETWEEN_FRAMES = 50 * US  # the test's own gap, with the clock high


def frame(clk, data, byte, start, khz, late, bad_parity=False, stop=1):
    """One frame's events and its end. The data moves as soon as it may, or with `late` as late as it may."""
    bits = [0] + bits_lsb([byte])
    bits += [(1 - sum(bits) % 2) ^ bad_parity, stop]
    half = 1e6 / khz / 2
    events = []
    for n, bit in enumerate(bits):
        fall = start + half + n * 2 * half
        change = fall - MARGIN if late else max(fall - half + MARGIN, fall - T1_MAX)
        events.append((change, {data: None if bit else 0}))  # open collector
        events.append((fall, {clk: 0}))
        events.append((fall + half, {clk: None}))
    end = start + 2 * half * len(bits)
    events.append((end + MARGIN, {data: None}))
    return events, end + BETWEEN_FRAMES
