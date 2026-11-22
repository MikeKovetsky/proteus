# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""UART, 8N1: the line idles high; a low start bit, 8 data bits LSB first, a high stop bit, all one bit long."""

from .wave import bits_lsb, bytes_lsb

TOLERANCE = 0.02  # of a bit, for every edge, counted from the frame's start edge


def decode(line, baud, since):
    """The bytes on the line, and the time span they took."""
    bit = 1e9 / baud
    assert line.at(since) == 1, "UART: the line is not idle high at the start"
    data, starts, t = [], [], since
    while True:
        falls = [at for at, level in line.changes(t) if level == 0]
        if not falls:
            break
        start = falls[0]
        if starts:
            assert start - starts[-1] >= (10 - TOLERANCE) * bit, (
                "UART: start bits only %.1f ns apart at %.0f ns: a short stop bit" % (start - starts[-1], start))
        for at, _ in line.changes(start, start + (10 - TOLERANCE) * bit):
            error = at - start - round((at - start) / bit) * bit
            assert abs(error) <= TOLERANCE * bit, "UART: an edge is %.1f ns off the %d baud grid" % (error, baud)
        bits = [line.at(start + (n + 0.5) * bit) for n in range(10)]
        assert bits[0] == 0, "UART: no start bit at %.0f ns" % start
        assert bits[9] == 1, "UART: the stop bit at %.0f ns is low" % start
        data += bytes_lsb(bits[1:9])
        starts.append(start)
        t = start + 9.5 * bit
    return bytes(data), (starts[0], starts[-1] + 10 * bit) if starts else None


def send(pin, data, baud, start):
    """Back-to-back frames, each start bit straight after a stop bit. Returns the events and their end."""
    bit, events, t = 1e9 / baud, [], start
    for byte in data:
        for level in [0] + bits_lsb([byte]) + [1]:
            events.append((t, {pin: level}))
            t += bit
    return events, t
