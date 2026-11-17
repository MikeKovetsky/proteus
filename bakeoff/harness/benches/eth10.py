"""10 Mbit Ethernet scenarios. See bakeoff/README.md for what each one asks of the chip."""
from __future__ import annotations

import math
from typing import Callable, List, Tuple

from ..chip import CH_RX, CH_STATUS, CH_TX, Chip
from ..codec import crc32_eth, eth_frame_bytes, manchester_encode
from ..sim import MASK, Bench, Result, Sim, payload
from ..stim import Stimulus, bits_lsb_first, bytes_lsb_first, first_difference, run, run_until

HALF = 3            # cycles in half a Manchester bit
BIT = 2 * HALF
IDLE_MIN, IDLE_MAX = 12, 24  # how long TX+ stays high after the last bit
FRAME_GAP = 576

# One sent frame as the decoder saw it: (first cycle, the cycle its last bit ends, bytes, problems).
SentFrame = Tuple[int, int, bytes, List[str]]


def decode_eth_tx(wave: List[int], plus: int, minus: int) -> List[SentFrame]:
    """Find every frame on TX+ and TX-, and judge each one cycle by cycle.

    A frame starts at the first cycle either line is high. From there every 6 cycles must be one Manchester
    bit: 3 cycles at one level and 3 at the other, with TX- the complement of TX+. The bits stop where that
    pattern does, and what follows must be TX+ high and TX- low for 12 to 24 cycles, then both low.
    """
    p_mask, m_mask = 1 << plus, 1 << minus
    both = p_mask | m_mask
    frames: List[SentFrame] = []
    n = len(wave)
    c = 0
    while c < n:
        if not wave[c] & both:
            c += 1
            continue
        start = c
        bits: List[int] = []
        while c + BIT <= n:
            window = wave[c : c + BIT]
            first = [1 if w & p_mask else 0 for w in window[:HALF]]
            second = [1 if w & p_mask else 0 for w in window[HALF:]]
            complement = all(bool(w & p_mask) != bool(w & m_mask) for w in window)
            if not (complement and len(set(first)) == 1 and len(set(second)) == 1 and first[0] != second[0]):
                break
            bits.append(second[0])  # a 1 is low then high
            c += BIT
        end = c
        problems: List[str] = []
        high = 0
        while c < n and wave[c] & p_mask and not wave[c] & m_mask:
            high += 1
            c += 1
        if bits and c < n and not wave[c] & both and high:
            if not IDLE_MIN <= high <= IDLE_MAX:
                problems.append(
                    "TX+ stays high for %d cycles after the last bit, %d to %d allowed" % (high, IDLE_MIN, IDLE_MAX)
                )
        elif bits and c < n and not wave[c] & both:
            problems.append(
                "both lines go low at cycle %d, straight after bit %d: TX+ must first stay high for %d to %d cycles"
                % (end, len(bits) - 1, IDLE_MIN, IDLE_MAX)
            )
        else:
            shown = wave[end : end + BIT]
            problems.append(
                "bit %d at cycle %d is not a Manchester bit: each half is 3 cycles and TX- is the complement of TX+ "
                "(TX+ %s, TX- %s)"
                % (
                    len(bits),
                    end,
                    "".join("1" if w & p_mask else "0" for w in shown),
                    "".join("1" if w & m_mask else "0" for w in shown),
                )
            )
            # Skip the rest of this burst: carry on at the next 30 cycles of idle.
            quiet = 0
            while c < n and quiet < 30:
                quiet = quiet + 1 if not wave[c] & both else 0
                c += 1
        if len(bits) % 8:
            problems.append("%d bits sent, not a whole number of bytes" % len(bits))
        frames.append((start, end, bytes_lsb_first(bits), problems))
    return frames


def describe_wire_difference(frame: bytes, got: bytes) -> str:
    """Compare what was sent with preamble + frame + FCS, and name the part that is wrong. '' when equal."""
    want = eth_frame_bytes(frame)
    if got == want:
        return ""
    for index, (w, g) in enumerate(zip(want, got)):
        if w != g:
            if index < 8:
                return "preamble byte %d: expected %02x, got %02x" % (index, w, g)
            if index < 8 + len(frame):
                return "frame byte %d: host gave %02x, got %02x" % (index - 8, w, g)
            return "FCS is %s, expected %s" % (got[8 + len(frame) : 12 + len(frame)].hex(), want[-4:].hex())
    return "%d bytes sent after the preamble, expected %d (the frame and its FCS)" % (len(got) - 8, len(want) - 8)


class Eth10Tx(Bench):
    """The chip sends two Ethernet frames (60 and 300 bytes) with preamble, SFD and FCS, Manchester coded.

    Both host messages are queued at reset, so the chip itself has to keep the frames apart.

    Lenient readings, where the README is silent:
    1. "At least 576 cycles apart" is measured from the end of the first frame's last bit to the first
       cycle of the second frame's preamble, so the 12 to 24 cycles of TX+ high count towards the gap.
    2. Idle is both lines low, whether the chip drives them low or leaves them to the pull-downs.
    3. The 12 to 24 cycles of TX+ high are counted from the end of the last bit. When the last bit is a 1,
       its high half comes before them.
    4. The chip may start whenever it likes: the run ends when two frames have gone by, or after
       `patience` cycles.

    "Every half bit is exactly 3 cycles" is taken at its word: there is no tolerance.
    """

    name = "eth10_tx"
    pins = {"TX+": 0, "TX-": 1}
    patience = 120000

    def run(self, make_chip: Callable[[], Chip], image: bytes) -> Result:
        plus, minus = self.pins["TX+"], self.pins["TX-"]
        both = (1 << plus) | (1 << minus)
        frames = [payload(80, 60), payload(81, 300)]
        sim = Sim(make_chip(), image, pull=MASK & ~both)
        for frame in frames:
            sim.send(CH_TX, len(frame).to_bytes(2, "little") + frame)
        seen = [0, 0]  # bursts started, and cycles of idle since the last one

        def finished(s: Sim) -> bool:
            if not s.wave:
                return False
            if s.wave[-1] & both:
                if seen[1]:
                    seen[0] += 1
                seen[1] = 0
            else:
                seen[1] += 1
            return seen[0] >= len(frames) and seen[1] >= 200

        seen[1] = 1  # the bus starts idle
        run_until(sim, finished, self.patience)
        sent = decode_eth_tx(sim.wave, plus, minus)
        problems: List[str] = []
        if len(sent) < len(frames):
            problems.append("%d frames on the wire in %d cycles, expected %d" % (len(sent), sim.cycle, len(frames)))
        for index, (frame, (start, end, got, found)) in enumerate(zip(frames, sent)):
            label = "frame %d (%d bytes)" % (index, len(frame))
            difference = describe_wire_difference(frame, got)
            if found:
                problems.append("%s: %s" % (label, found[0]))
            elif difference:
                problems.append("%s: %s" % (label, difference))
            if index and start - sent[index - 1][1] < FRAME_GAP:
                problems.append(
                    "%s starts %d cycles after the last bit of the frame before, needs %d"
                    % (label, start - sent[index - 1][1], FRAME_GAP)
                )
        if len(sent) > len(frames):
            problems.append("%d bursts on the wire, expected %d frames" % (len(sent), len(frames)))
        if sim.contention:
            problems.append("%d pin-cycles of contention" % sim.contention)
        return Result(self.name, not problems, "; ".join(problems[:3]), sim.cycle)


def send_eth_frame(stim: Stimulus, pin: int, wire: bytes, start: float, period: float) -> float:
    """Schedule `wire` (preamble and SFD included) as Manchester on `pin`. Return when the line goes idle.

    After the last bit the line is high for three bit times, as a sender's end-of-frame marker is, then low.
    """
    half = period / 2
    level = 0
    halves = manchester_encode(bits_lsb_first(wire))
    for k, want in enumerate(halves):
        if want != level:
            stim.at(start + k * half, pin, want)
            level = want
    end = start + len(halves) * half
    stim.at(end, pin, 1)
    stim.at(end + 3 * period, pin, 0)
    return end + 3 * period


class Eth10Rx(Bench):
    """The bench sends three Manchester frames on P2 and the chip reports each one.

    Lenient readings, where the README is silent:
    1. The frames are 64, 65 and 300 bytes with their FCS, at +200 ppm, -200 ppm and +200 ppm. Each starts
       at a fixed phase against the chip's clock, picked so that every frame has at least one half bit of
       2 or 4 cycles.
    2. Each frame ends as the README's sender does: the line stays high for three bit times (18 cycles)
       after the last bit, then goes low.
    3. The first frame starts about 3,000 cycles after reset and the others about 2,000 cycles after the
       frame before. P2 has a pull-down in this bench, so the chip sees the line low from its first clock.
    4. The length bytes on channel 2 count the bytes pushed to channel 1 for that frame, FCS included.
    5. The flipped bit of the third frame is in its payload, and the chip pushes the bytes as they came.
    6. The chip may take `patience` cycles after the last frame to deliver.
    """

    name = "eth10_rx"
    pins = {"RX": 2}
    startup = 3000
    gap = 2000
    patience = 20000

    def run(self, make_chip: Callable[[], Chip], image: bytes) -> Result:
        pin = self.pins["RX"]
        frames = []
        for seed, length in ((90, 64), (91, 65), (92, 300)):
            body = payload(seed, length - 4)
            frames.append(body + crc32_eth(body).to_bytes(4, "little"))
        broken = bytearray(frames[2])
        broken[100] ^= 0x08
        frames[2] = bytes(broken)
        # A pull-down on RX, so the chip sees the line low from its very first clock.
        sim = Sim(make_chip(), image, pull=MASK & ~(1 << pin))
        stim = Stimulus()
        stim.at(0, pin, 0)
        t = float(self.startup)
        for frame, ppm, phase in zip(frames, (200, -200, 200), (1.7, 4.3, 2.55)):
            start = math.ceil(t) + phase
            t = send_eth_frame(stim, pin, b"\x55" * 7 + b"\xd5" + frame, start, BIT * (1 + ppm * 1e-6)) + self.gap
        want_data = b"".join(frames)
        want_status = b"".join(
            bytes([1 if index == 2 else 0]) + len(frame).to_bytes(2, "little") for index, frame in enumerate(frames)
        )
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
            index = next((i for i, (w, g) in enumerate(zip(want_data, got)) if w != g), min(len(want_data), len(got)))
            frame_index, offset = 0, index
            while frame_index < len(frames) - 1 and offset >= len(frames[frame_index]):
                offset -= len(frames[frame_index])
                frame_index += 1
            problems.append("channel 1 %s (frame %d, its byte %d)" % (difference, frame_index, offset))
        difference = first_difference(want_status, sim.received(CH_STATUS), "expected")
        if difference:
            problems.append("channel 2 %s (three bytes per frame: status, length low, length high)" % difference)
        if sim.contention:
            problems.append("%d pin-cycles of contention" % sim.contention)
        return Result(self.name, not problems, "; ".join(problems[:3]), sim.cycle)


BENCHES = [Eth10Tx(), Eth10Rx()]
