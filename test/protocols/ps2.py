# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""PS/2 device to host, from IBM's keyboard interface reference.

The device makes both the clock (10 to 16.7 kHz) and the data. A frame is a 0, 8 data bits LSB first, odd
parity and a 1, each valid on a falling clock edge. Data moves at least 5 us after the clock rises and at
least 5 us before it falls.
"""

from .wave import US, bits_lsb

MARGIN = 5 * US


def frame(clk, data, byte, start, khz, late, bad_parity=False, stop=1):
    """One frame's events and its end. The data moves as soon as it may, or with `late` as late as it may."""
    bits = [0] + bits_lsb([byte])
    bits += [(1 - sum(bits) % 2) ^ bad_parity, stop]
    half = 1e6 / khz / 2
    events = []
    for n, bit in enumerate(bits):
        fall = start + half + n * 2 * half
        change = fall - MARGIN if late else fall - half + MARGIN
        events.append((change, {data: None if bit else 0}))  # open collector
        events.append((fall, {clk: 0}))
        events.append((fall + half, {clk: None}))
    end = start + 2 * half * len(bits)
    events.append((end + MARGIN, {data: None}))
    return events, end + 50 * US  # the clock is high for 50 us before another frame
