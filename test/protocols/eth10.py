# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""10BASE-T from IEEE 802.3: clause 3 (the frame and its FCS), 7 (Manchester) and 14 (the twisted-pair MAU).

A bit is 100 ns. A 1 is low then high, a 0 high then low, on the differential signal TX+ less TX-. Bytes go
LSB first. After the last bit comes TP_IDL, a positive level of at least 250 ns, then silence.
"""

from .wave import EPS, US, bits_lsb, bytes_lsb, change_times

BIT, HALF = 100.0, 50.0
PPM = 100  # of each station's clock
TRANSMIT_JITTER = 3.5  # at a transmitter's output, through the cable model
RECEIVE_JITTER = 13.5  # what a receiver must take
IDL_FROM_EDGE = 250.0  # the last transition to the end of TP_IDL, at least
IDL_AFTER_BIT = (200.0, 400.0)  # the scenario's: 12 to 24 clocks after the last bit
IFG = 9.6 * US
PREAMBLE = b"\x55" * 7 + b"\xd5"


def send(pin, frame, start, ppm=0, jitter=None):
    """One station's signal for a frame (FCS included), as a comparator puts it on one pin that idles low.
    `jitter(half)` moves the edge at a half-bit boundary. Returns the events and the end of the last bit."""
    halves = []
    for bit in bits_lsb(PREAMBLE + frame):
        halves += [1 - bit, bit]
    half = HALF * (1 + ppm * 1e-6)
    events, level = [], 0
    for n, following in enumerate(halves + [1]):  # the 1 is TP_IDL
        if following != level:
            events.append((start + n * half + (jitter(n) if jitter else 0.0), {pin: following}))
            level = following
    end = start + len(halves) * half
    events.append((end + 300, {pin: 0}))
    return events, end


def decode(line, since):
    """Every frame after the preamble and SFD, FCS included, after checking both wires and every edge."""
    tp, tn = line
    assert (tp.at(since), tn.at(since)) == (0, 0), "Ethernet: both wires are not low when idle"
    times = change_times(line, since)
    levels = []
    for t in times:
        pair = (tp.at(t), tn.at(t))
        assert pair != (1, 1), "Ethernet: both wires high at %.0f ns" % t
        levels.append(pair[0] - pair[1])

    frames, i, previous_end = [], 0, None
    while i < len(times):
        t0 = times[i]
        assert levels[i] == -1, "Ethernet: a frame does not open with the first half of a 1"
        if previous_end is not None:
            assert t0 - previous_end >= IFG - EPS, "Ethernet: an interframe gap of %.1f ns" % (t0 - previous_end)
        last = i
        while last < len(times) and levels[last] != 0:
            last += 1
        assert last < len(times), "Ethernet: no idle after a frame"
        idle = times[last]
        for t in times[i:last]:
            error = t - t0 - round((t - t0) / HALF) * HALF
            assert abs(error) <= TRANSMIT_JITTER, "Ethernet: an edge is %.1f ns off the half-bit grid" % error

        def level(t):
            return tp.at(t) - tn.at(t)

        bits = []
        while True:
            first, second = (level(t0 + len(bits) * BIT + at) for at in (HALF / 2, HALF * 1.5))
            if first == second:
                assert first == 1, "Ethernet: a code violation after %d bits" % len(bits)
                break
            assert first and second, "Ethernet: the wires are not complements at bit %d" % len(bits)
            bits.append(1 if second == 1 else 0)
        end = t0 + len(bits) * BIT
        assert times[last - 1] <= end + EPS, "Ethernet: an edge inside TP_IDL"
        assert IDL_AFTER_BIT[0] - EPS <= idle - end <= IDL_AFTER_BIT[1] + EPS, (
            "Ethernet: TP_IDL lasts %.1f ns" % (idle - end))
        assert idle - times[last - 1] >= IDL_FROM_EDGE - EPS, "Ethernet: TP_IDL ends too soon after the last edge"
        sent = bytes_lsb(bits)
        assert sent[:8] == PREAMBLE, "Ethernet: preamble and SFD are %s" % sent[:8].hex()
        frames.append(sent[8:])
        previous_end, i = idle, last + 1
    return frames
