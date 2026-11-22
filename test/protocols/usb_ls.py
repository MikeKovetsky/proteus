# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Low-speed USB, from the USB 2.0 specification (test/README.md, "Where the limits come from"): 7.1.7 to 7.1.18,
its timing table 7-10, the packet IDs of table 8-1, and 8.3.5 for the CRC.

1.5 Mbit/s. J is D- high and D+ low, K the opposite, SE0 both low. A packet is SYNC (KJKJKJKK), bytes LSB
first with a 0 stuffed after six 1s, NRZI (a 0 toggles the line), and EOP: two bits of SE0, then one of J.
"""

from .wave import CLK, EPS, bits_lsb, bytes_lsb, change_times

BIT = 2000 / 3  # ns
RATE_TOLERANCE = 0.015  # TLDRATE, 1.4775 to 1.5225 Mb/s
SOURCE_JITTER_NEXT, SOURCE_JITTER_PAIRED = 25.0, 14.0  # TDDJ1, TDDJ2: the stricter, downstream, figures
DEVICE_JITTER_NEXT, DEVICE_JITTER_PAIRED = 75.0, 45.0  # TDJR1, TDJR2: what a device's receiver must take
HOST_JITTER_NEXT, HOST_JITTER_PAIRED = 152.0, 200.0  # TUJR1, TUJR2: what a host's receiver must take
GRID = 2 * CLK  # the scenario's: every edge this close to the grid from SOP. It holds the rate inside TLDRATE
EOP_SENT = (1250.0, 1500.0)  # TLEOPT
EOP_ACCEPTED = 670.0  # TLEOPR
TRANSITION_SE0 = 210.0  # TLST: an SE0 this long inside a transition is not an EOP
PACKET_GAP = 2 * BIT  # 7.1.18.1: from SE0-to-J to the next J-to-K, at least
DATA0, DATA1, ACK = 0xC3, 0x4B, 0xD2  # table 8-1, each with its check nibble
J, K, SE0 = (0, 1), (1, 0), (0, 0)


def stuff(bits):
    out, ones = [], 0
    for bit in bits:
        out.append(bit)
        ones = ones + 1 if bit else 0
        if ones == 6:
            out.append(0)
            ones = 0
    return out


def line_states(packet):
    """J or K for every bit from SYNC to the last bit before EOP, stuffed bits included."""
    state, out = J, []
    for bit in stuff([0] * 7 + [1] + bits_lsb(packet)):
        if not bit:
            state = K if state == J else J
        out.append(state)
    return out


def send(pins, packet, start, rate=1.0, jitter=None, skew=0.0, eop=2 * BIT):
    """A packet's events and the time of its SE0-to-J. `jitter(n)` moves transition n. `skew` is the SE0 inside
    every transition: the falling line leads and the rising one lags by half of it."""
    dp, dm = pins
    bit, events, state, n = BIT / rate, [], J, 0
    states = line_states(packet)
    for i, following in enumerate(states):
        if following == state:
            continue
        t = start + i * bit + (jitter(n) if jitter else 0.0)
        rising, falling = (dp, dm) if following == K else (dm, dp)
        if skew:
            events += [(t - skew / 2, {falling: 0}), (t + skew / 2, {rising: 1})]
        else:
            events.append((t, {falling: 0, rising: 1}))
        state, n = following, n + 1
    t = start + len(states) * bit
    events += [(t, {dp: 0, dm: 0}), (t + eop, {dm: 1}), (t + eop + bit, {dp: None, dm: None})]
    return events, t + eop


def decode(line, driven, since):
    """Every packet sent (the bytes after SYNC), after checking timing, NRZI, stuffing and EOP."""
    dp, dm = line
    times = change_times(line, since)
    states = [(t, (dp.at(t), dm.at(t))) for t in times]
    assert all(state in (J, K, SE0) for _, state in states), "USB: SE1 on the bus"
    assert (dp.at(since), dm.at(since)) == J, "USB: the bus does not idle at J"
    packets, i, previous_end = [], 0, None
    while i < len(states):
        t0, state = states[i]
        assert state == K, "USB: SOP is not J to K at %.0f ns" % t0
        assert all(w.at(t0) is not None for w in driven), "USB: SOP is not driven"
        if previous_end is not None:
            assert t0 - previous_end >= PACKET_GAP - EPS, "USB: under 2 bit times between packets"
        edges = [t0]
        i += 1
        while i < len(states) and states[i][1] != SE0:
            edges.append(states[i][0])
            i += 1
        assert i + 1 < len(states), "USB: a packet with no EOP"
        se0 = states[i][0]
        back_to_j, state = states[i + 1]
        i += 2
        edges.append(se0)
        cells = [round((t - t0) / BIT) for t in edges]
        for n, (t, cell) in enumerate(zip(edges, cells)):
            assert abs(t - t0 - cell * BIT) <= GRID + EPS, "USB: an edge is %.1f ns off the bit grid" % (
                t - t0 - cell * BIT)
            for back, limit in ((1, SOURCE_JITTER_NEXT), (2, SOURCE_JITTER_PAIRED)):
                if n >= back:
                    error = t - edges[n - back] - (cell - cells[n - back]) * BIT
                    assert abs(error) <= limit + EPS, "USB: source jitter of %.1f ns, over %g" % (error, limit)
        assert state == J, "USB: EOP does not end in J"
        assert EOP_SENT[0] - EPS <= back_to_j - se0 <= EOP_SENT[1] + EPS, (
            "USB: EOP has %.1f ns of SE0" % (back_to_j - se0))
        assert all(w.at(back_to_j) is not None for w in driven), "USB: the J after EOP is not driven"
        for wave in driven:
            released = [t for t, level in wave.changes(back_to_j) if level is None]
            assert released and abs(released[0] - back_to_j - BIT) <= GRID + EPS, (
                "USB: J after EOP is not driven for one bit time, then released")
            assert not any(level is None for _, level in wave.changes(t0, back_to_j)), (
                "USB: the bus is let go inside a packet")
        previous_end = back_to_j
        packets.append(unpack([(dp.at(t0 + (n + 0.5) * BIT), dm.at(t0 + (n + 0.5) * BIT))
                               for n in range(cells[-1])]))
    return packets


def unpack(cells):
    assert cells[:8] == [K, J, K, J, K, J, K, K], "USB: SYNC is not KJKJKJKK"
    bits, ones, last = [], 1, cells[7]  # the 1 that ends SYNC counts towards stuffing
    for state in cells[8:]:
        bit = 1 if state == last else 0
        last = state
        if ones == 6:
            assert bit == 0, "USB: seven 1s in a row, a stuffed bit is missing"
            ones = 0
            continue
        bits.append(bit)
        ones = ones + 1 if bit else 0
    assert ones < 6, "USB: no stuffed bit before EOP"
    return bytes_lsb(bits)
