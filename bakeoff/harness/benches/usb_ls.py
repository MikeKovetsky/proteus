"""Low-speed USB scenarios. See bakeoff/README.md for what each one asks of the chip."""
from __future__ import annotations

import random
from typing import Callable, List, Optional, Tuple

from ..chip import CH_RX, CH_STATUS, CH_TX, Chip
from ..codec import USB_SYNC_BITS, crc16_usb, nrzi_decode, usb_packet_levels, usb_unstuff
from ..sim import MASK, Bench, Result, Sim, payload
from ..stim import Stimulus, bytes_lsb_first, first_difference, first_of, run, run_until

PINS = {"D+": 0, "D-": 1}
BIT = 40
PID_DATA0, PID_DATA1 = 0xC3, 0x4B
# Line states as (D- << 1) | D+ when D+ is P0 and D- is P1.
SE0, K, J, SE1 = 0, 1, 2, 3
STATE_NAMES = {SE0: "SE0", K: "K", J: "J", SE1: "SE1"}


def with_crc(pid: int, data: bytes) -> bytes:
    """Everything after SYNC for a data packet: the PID, the data and the CRC-16, low byte first."""
    return bytes([pid]) + data + crc16_usb(data).to_bytes(2, "little")


def decode_usb_tx(wave: List[int], oe_wave: List[int]) -> Tuple[Optional[bytes], List[str]]:
    """Decode the first packet the chip sent on P0 and P1, and judge its timing. Return (bytes after SYNC, problems)."""
    states = [w & 3 for w in wave]
    start = next((c for c, state in enumerate(states) if state != J), None)
    if start is None:
        return None, ["no packet: the bus stayed at J for %d cycles" % len(wave)]
    problems: List[str] = []

    # Sample the middle of every bit time until SE0.
    levels: List[int] = []
    c = start + BIT // 2
    while c < len(states) and states[c] in (J, K):
        levels.append(1 if states[c] == J else 0)
        c += BIT
    if c >= len(states):
        return None, ["the packet that starts at cycle %d never reaches SE0" % start]
    if states[c] == SE1:
        return None, ["both lines are high (SE1) at cycle %d, bit %d of the packet" % (c, len(levels))]
    n = len(levels)
    if not n:
        return None, ["the bus goes from J straight to SE0 at cycle %d, with no SYNC" % start]
    se0_bits = 0
    while c < len(states) and states[c] == SE0:
        se0_bits += 1
        c += BIT
    if se0_bits != 2:
        problems.append("SE0 lasts %d bit times after bit %d, expected 2" % (se0_bits, n))
    if c < len(states) and states[c] != J:
        problems.append("the bus is %s after SE0 at cycle %d, expected J" % (STATE_NAMES[states[c]], c))
    j_bit = start + (n + 2) * BIT + BIT // 2
    if j_bit < len(oe_wave) and not oe_wave[j_bit] & 3:
        problems.append("chip does not drive J for a bit time after SE0 (nothing driven at cycle %d)" % j_bit)
    end = start + (n + 3) * BIT  # where the chip should let go
    still = next((x for x in range(end + BIT, len(oe_wave)) if oe_wave[x] & 3), None)
    if still is not None:
        problems.append("chip still drives the bus at cycle %d, after the packet ended at %d" % (still, end))

    # Every edge of either line must sit within 2 cycles of the 40-cycle grid that starts with SYNC.
    off_grid: List[str] = []
    stray: List[str] = []
    for x in range(start + 1, len(wave)):
        changed = (wave[x] ^ wave[x - 1]) & 3
        if not changed:
            continue
        name = "D+" if changed & 1 else "D-"
        if x > end + 2:
            stray.append("%s changes at cycle %d, after the packet ended at %d" % (name, x, end))
            continue
        offset = (x - start) % BIT
        offset = offset - BIT if offset > BIT // 2 else offset
        if abs(offset) > 2:
            off_grid.append(
                "%s edge at cycle %d is %+d cycles off the grid (bit %d of the packet)"
                % (name, x, offset, round((x - start) / BIT))
            )
    problems += first_of(off_grid) + first_of(stray)

    bits = nrzi_decode(levels, 1)
    if bits[:8] != USB_SYNC_BITS:
        shown = "".join("J" if level else "K" for level in levels[:8])
        problems.insert(0, "the packet starts %s, expected SYNC KJKJKJKK" % shown)
        return None, problems
    unstuffed, error = usb_unstuff(bits)
    body_bits = unstuffed[8:]
    if len(body_bits) % 8:
        problems.insert(0, "%d bits after SYNC once unstuffed, not a whole number of bytes" % len(body_bits))
    if error is not None:
        problems.insert(0, "seven 1s in a row at bit %d: a 0 must be stuffed after six" % error)
    return bytes_lsb_first(body_bits), problems


class UsbLsTx(Bench):
    """The chip sends a low-speed USB packet from `[n][PID][n-1 data bytes]`.

    Lenient readings, where the README is silent or ambiguous:
    1. "Three packets" are three runs with the same image, one packet each, so the program need not loop.
    2. The packet with no data is `[1][0xC3]`. The README's rule gives it a CRC-16 of 0x0000, and a
       handshake packet in real USB has no CRC, so both `C3 00 00` and a bare `C3` pass.
    3. The PIDs are real ones (DATA0 0xC3 and DATA1 0x4B), so whether the 1 that ends SYNC counts towards
       the six 1s does not matter.
    4. D+ and D- may move up to 2 cycles apart at a J/K change, as long as each edge is within 2 cycles of
       the grid. The grid starts at the first cycle the bus leaves J. Bits are read in the middle of each
       bit time.
    5. "J for one bit time, then stops driving": the chip must be driving at least one line in the middle
       of that bit, and neither line from one bit time after it should have let go. Before the packet it
       may drive J or leave the pulls to it.
    6. The chip may start whenever it likes within `patience` cycles of reset.
    """

    name = "usb_ls_tx"
    pins = PINS
    patience = 50000

    def run(self, make_chip: Callable[[], Chip], image: bytes) -> Result:
        packets = (
            ("no data", PID_DATA0, b""),
            ("eight ff", PID_DATA1, b"\xff" * 8),
            ("five bytes", PID_DATA0, payload(60, 5)),
        )
        problems: List[str] = []
        cycles = 0
        for label, pid, data in packets:
            sim = Sim(make_chip(), image, pull=MASK & ~1)
            sim.send(CH_TX, bytes([1 + len(data), pid]) + data)
            se0 = [0, -1]  # how long the bus has been at SE0, and when a real SE0 (half a bit or more) last ended

            def finished(s: Sim) -> bool:
                if not s.wave:
                    return False
                se0[0] = se0[0] + 1 if s.wave[-1] & 3 == SE0 else 0
                if se0[0] >= BIT // 2:
                    se0[1] = s.cycle
                return se0[1] >= 0 and s.cycle - se0[1] > 6 * BIT

            run_until(sim, finished, self.patience)
            run(sim, 4 * BIT)
            cycles += sim.cycle
            body, found = decode_usb_tx(sim.wave, sim.oe_wave)
            if body is not None:
                want = with_crc(pid, data)
                accepted = [want] if data else [want, bytes([pid])]
                if body not in accepted:
                    if body[: 1 + len(data)] == want[: 1 + len(data)] and len(body) == len(want):
                        found.append("CRC bytes are %s, expected %s" % (body[-2:].hex(), want[-2:].hex()))
                    else:
                        found.append(first_difference(want, body, "expected") or "wrong packet")
            if sim.contention:
                found.append("%d pin-cycles of contention" % sim.contention)
            problems += ["%s: %s" % (label, problem) for problem in found]
        return Result(self.name, not problems, "; ".join(problems[:3]), cycles)


def send_usb_packet(stim: Stimulus, body: bytes, start: float, bit: float) -> float:
    """Schedule SYNC, `body` and the end of packet on D+ and D-. Return the time the bench lets go."""
    dp, dm = PINS["D+"], PINS["D-"]
    levels = usb_packet_levels(body)
    for k, level in enumerate(levels):
        if k == 0 or level != levels[k - 1]:
            stim.at(start + k * bit, dp, 0 if level else 1)
            stim.at(start + k * bit, dm, 1 if level else 0)
    n = len(levels)
    stim.at(start + n * bit, dp, 0)  # SE0 for two bit times
    stim.at(start + n * bit, dm, 0)
    stim.at(start + (n + 2) * bit, dm, 1)  # J for one
    stim.at(start + (n + 3) * bit, dp, None)
    stim.at(start + (n + 3) * bit, dm, None)
    return start + (n + 3) * bit


class UsbLsRx(Bench):
    """The bench sends four low-speed USB packets and the chip reports each one.

    Lenient readings, where the README is silent:
    1. Every packet is a data packet: PID, data, CRC-16. The first has 8 bytes of data and runs 1.5% fast
       (39.41 cycles per bit). The second has 8 bytes with long runs of 1s, so it is heavily stuffed, and
       runs 1.5% slow (40.61 cycles). The third (3 bytes) and fourth (5 bytes) are nominal, and the fourth
       has one bit of its CRC flipped.
    2. "Byte count" is the number of bytes pushed to channel 1 for that packet: PID, data and CRC.
    3. The bench drives SE0 for two bit times and J for one, then lets the pulls hold J. The first packet
       starts about 3,000 cycles after reset and the others about 2,000 cycles after the one before, each
       plus a seeded random phase of up to one bit time.
    4. D+ and D- change in the same cycle. The chip may take `patience` cycles after the last packet.

    "The chip never drives" is taken at its word: driving D+ or D- at all fails the bench.
    """

    name = "usb_ls_rx"
    pins = PINS
    startup = 3000
    gap = 2000
    patience = 20000

    def run(self, make_chip: Callable[[], Chip], image: bytes) -> Result:
        stuffed = b"\xff\xff\x00\xff\x7e\xff" + payload(62, 2)
        bodies = [
            with_crc(PID_DATA0, payload(61, 8)),
            with_crc(PID_DATA1, stuffed),
            with_crc(PID_DATA0, payload(63, 3)),
            with_crc(PID_DATA1, payload(64, 5)),
        ]
        bodies[3] = bodies[3][:-2] + bytes([bodies[3][-2] ^ 0x04]) + bodies[3][-1:]
        rates = (1.015, 0.985, 1.0, 1.0)
        rng = random.Random(1105)
        sim = Sim(make_chip(), image, pull=MASK & ~1)
        stim = Stimulus()
        t = float(self.startup)
        for body, rate in zip(bodies, rates):
            t = send_usb_packet(stim, body, t + rng.uniform(0, BIT), BIT / rate) + self.gap
        want_data = b"".join(bodies)
        want_status = b"".join(bytes([1 if i == 3 else 0, len(body)]) for i, body in enumerate(bodies))
        run_until(
            sim,
            lambda s: s.cycle >= t
            and len(s.from_chip[CH_RX]) >= len(want_data)
            and len(s.from_chip[CH_STATUS]) >= len(want_status),
            t + self.patience,
            stim,
        )
        run(sim, 500, stim)

        problems: List[str] = []
        got = sim.received(CH_RX)
        difference = first_difference(want_data, got)
        if difference:
            # Say which packet the first wrong byte belongs to.
            index = next((i for i, (w, g) in enumerate(zip(want_data, got)) if w != g), min(len(want_data), len(got)))
            packet, offset = 0, index
            while packet < len(bodies) - 1 and offset >= len(bodies[packet]):
                offset -= len(bodies[packet])
                packet += 1
            problems.append("channel 1 %s (packet %d, its byte %d)" % (difference, packet, offset))
        difference = first_difference(want_status, sim.received(CH_STATUS), "expected")
        if difference:
            problems.append("channel 2 %s (two bytes per packet: status, byte count)" % difference)
        drove = next((c for c, oe in enumerate(sim.oe_wave) if oe & 3), None)
        if drove is not None:
            problems.append("chip drives D+ or D- at cycle %d, and it must never drive" % drove)
        if sim.contention:
            problems.append("%d pin-cycles of contention" % sim.contention)
        return Result(self.name, not problems, "; ".join(problems[:3]), sim.cycle)


BENCHES = [UsbLsTx(), UsbLsRx()]
