# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Why gap_eth10_rx_jitter cannot close in a program for all the jitter it names.

    python3 eth10_jitter_limit.py

The chip sees a pad once a clock, every 16.7 ns. For 13.5 ns of jitter, and for less, this builds two frames
of 64 bytes, each with a good FCS and sent by protocols/eth10.py with exact clocks and every edge inside that
jitter, that are the same level at every one of those samples. Their data differ, so no program can tell which
was sent. Neither can RTL that samples as seldom. In both preambles the jitter alternates and averages to 0.
"""

from math import ceil

from protocols import crc, eth10
from protocols.wave import CLK, EPS, bits_lsb, bytes_lsb

RX = 2


def same_clock(a, b):
    return ceil((min(a, b) - EPS) / CLK) == ceil((max(a, b) + EPS) / CLK)  # and neither on a clock's edge


def placing(most):
    """A phase, how much earlier the second sender is, and the jitter where the two agree. Then every edge of
    one falls in the same clock as one of the other, though at two places they are half a bit apart."""
    lowest = max(0.0, eth10.HALF - 2 * most - CLK)
    for i in range(1, 10):
        earlier = lowest + i * (CLK - lowest) / 10
        for steady in (most - n * 0.02 for n in range(int(most / 0.02))):
            for phase in (1000 + n * 0.02 for n in range(int(CLK / 0.02))):
                late = phase + most
                early = phase - earlier + eth10.HALF - most
                if (same_clock(late, early) and same_clock(phase + steady, phase - earlier + steady)
                        and same_clock(phase - steady, phase - earlier - steady)):
                    return phase, earlier, steady
    return None


def two_frames():
    """Two bodies with one FCS, and the bits that differ. Every fourth bit of 0x33 starts 1 1 0 or 0 0 1, and
    its middle bit can be read either way. Some set of those flips leaves CRC-32 as it was."""
    body = [1, 1, 0, 0] * 120
    basis, zero = {}, crc.crc(crc.CRC32_ETHERNET, bytes(len(body) // 8))
    for site in range(1, len(body), 4):
        flip = [int(n == site) for n in range(len(body))]
        value, sites = crc.crc(crc.CRC32_ETHERNET, bytes_lsb(flip)) ^ zero, {site}
        while value and value.bit_length() in basis:
            other, used = basis[value.bit_length()]
            value, sites = value ^ other, sites ^ used
        if not value:
            second = [bit ^ (n in sites) for n, bit in enumerate(body)]
            return (crc.with_crc(crc.CRC32_ETHERNET, bytes_lsb(body)),
                    crc.with_crc(crc.CRC32_ETHERNET, bytes_lsb(second)), sites)
        basis[value.bit_length()] = value, sites
    raise AssertionError("no two frames with one FCS")


def check(most):
    placed = placing(most)
    assert placed, "found no pair at %g ns" % most
    phase, earlier, steady = placed
    first, second, sites = two_frames()
    start = len(bits_lsb(eth10.PREAMBLE))
    special = {}
    for site in sites:
        half = 2 * (start + site - 1)  # the first half of the bit before the one that differs
        special.update({(0, half + 2): most, (0, half + 3): most, (1, half + 3): -most, (1, half + 4): -most})
    waves = []
    for who, frame in enumerate((first, second)):
        def jitter(half):
            return special.get((who, half), steady if half // 2 % 2 else -steady)

        assert crc.crc(crc.CRC32_ETHERNET, frame[:-4]) == int.from_bytes(frame[-4:], "little"), "a bad FCS"
        events, _ = eth10.send(RX, frame, phase - who * earlier, jitter=jitter)
        fell, levels = events[-1]
        events = events[:-1] + [(fell + steady, levels)]  # the end of TP_IDL, jittered as the preamble is
        assert events == sorted(events, key=lambda event: event[0]), "edges out of order"
        samples, level, n = [], 0, 0
        for clock in range(ceil((events[-1][0] + 100) / CLK)):
            while n < len(events) and events[n][0] <= clock * CLK:
                level = events[n][1][RX]
                n += 1
            samples.append(level)
        waves.append(samples)
    assert first != second and waves[0] == waves[1], "the samples differ"
    print("%4.1f ns: two good frames of %d bytes, %d bits apart, the same at all %d samples "
          "(phases %.1f ns apart, %.2f ns of jitter in the preamble)"
          % (most, len(first), len(sites), len(waves[0]), earlier, steady))


if __name__ == "__main__":
    for jitter_ns in (eth10.RECEIVE_JITTER, 10.0, 8.4):
        check(jitter_ns)
