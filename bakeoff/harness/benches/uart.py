"""UART scenarios. See bakeoff/README.md for what each one asks of the chip."""
from __future__ import annotations

from typing import Callable, List, Tuple

from ..chip import CH_RX, CH_TX, CLK_HZ, Chip
from ..sim import Bench, Result, Sim, payload
from ..stim import Stimulus, first_difference, run, run_until


def decode_uart(wave: List[int], pin: int, bit: float, tolerance: float = 0.02) -> Tuple[bytes, List[str]]:
    """Decode 8N1 frames from `wave`, LSB first, and check where every edge falls.

    Each frame is sampled mid-bit from its own start edge. An edge inside a frame must sit within
    `tolerance` of a whole number of bit times from that start edge (never tighter than two cycles).
    """
    mask = 1 << pin
    data = bytearray()
    problems: List[str] = []
    n = len(wave)
    i = 1
    while i < n:
        if not (wave[i - 1] & mask and not wave[i] & mask):
            i += 1
            continue
        start = i
        end = start + int(round(10 * bit))
        if end >= n:
            problems.append("frame at cycle %d runs past the end of the capture" % start)
            break
        levels = [1 if wave[start + int((k + 0.5) * bit)] & mask else 0 for k in range(10)]
        if levels[9] != 1:
            problems.append("frame at cycle %d has no stop bit" % start)
        for j in range(start + 1, start + int(9.5 * bit)):
            if (wave[j] ^ wave[j - 1]) & mask:
                offset = j - start
                k = round(offset / bit)
                allowed = max(2.0, tolerance * k * bit)
                if abs(offset - k * bit) > allowed:
                    problems.append(
                        "edge at cycle %d is %.1f cycles off bit %d of the frame at %d" % (j, offset - k * bit, k, start)
                    )
        data.append(sum(levels[1 + b] << b for b in range(8)))
        i = start + int(9.5 * bit)
    return bytes(data), problems


class UartTx(Bench):
    name = "uart_tx"
    pins = {"TX": 0}
    baud = 115200

    def run(self, make_chip: Callable[[], Chip], image: bytes) -> Result:
        want = payload(1, 16)
        sim = Sim(make_chip(), image)
        sim.send(CH_TX, want)
        bit = CLK_HZ / self.baud
        # Room for every frame twice over, in case the chip leaves gaps between frames.
        sim.run(2 * 10 * bit * len(want) + 4000)
        got, problems = decode_uart(sim.wave, self.pins["TX"], bit)
        if sim.contention:
            problems.append("%d pin-cycles of contention" % sim.contention)
        if got != want:
            problems.insert(0, "sent %s, decoded %s" % (want.hex(), got.hex()))
        return Result(self.name, not problems, "; ".join(problems[:3]), sim.cycle)


def send_uart(stim: Stimulus, pin: int, data: bytes, start: float, bit: float, gap_bits: float = 0.0) -> float:
    """Schedule 8N1 frames on `pin`, LSB first, the first start edge at `start`. Return when the last ends.

    `bit` is the bit time in cycles and may be fractional. `gap_bits` is extra idle time after each stop bit.
    """
    t = start
    for byte in data:
        levels = [0] + [(byte >> b) & 1 for b in range(8)] + [1]
        for k, level in enumerate(levels):
            stim.at(t + k * bit, pin, level)
        t += (10 + gap_bits) * bit
    return t


class UartRx(Bench):
    """The bench sends 16 bytes back to back on P1, once 2% fast and once 2% slow.

    Lenient readings: "2% fast" is a baud rate of 115200 x 1.02 (a bit of 510.6 cycles) and "2% slow" is
    115200 x 0.98 (531.5 cycles). The line idles high for `startup` cycles before the first start bit.
    The chip may take up to `patience` cycles after the last stop bit to deliver the bytes. Each run uses
    its own 16 bytes. Channel 2 is ignored.
    """

    name = "uart_rx"
    pins = {"RX": 1}
    baud = 115200
    startup = 3000
    patience = 20000

    def run(self, make_chip: Callable[[], Chip], image: bytes) -> Result:
        rx = self.pins["RX"]
        problems: List[str] = []
        cycles = 0
        for label, rate, seed in (("2% fast", 1.02, 2), ("2% slow", 0.98, 3)):
            want = payload(seed, 16)
            sim = Sim(make_chip(), image)
            stim = Stimulus()
            stim.at(0, rx, 1)
            end = send_uart(stim, rx, want, self.startup, CLK_HZ / (self.baud * rate))
            run_until(
                sim, lambda s: s.cycle >= end and len(s.from_chip[CH_RX]) >= len(want), end + self.patience, stim
            )
            run(sim, 2000, stim)  # long enough to see a byte that should not be there
            cycles += sim.cycle
            difference = first_difference(want, sim.received(CH_RX))
            if difference:
                problems.append("%s: %s" % (label, difference))
            if sim.contention:
                problems.append("%s: %d pin-cycles of contention" % (label, sim.contention))
        return Result(self.name, not problems, "; ".join(problems[:3]), cycles)


class Uart3M(Bench):
    """3 Mbaud in both directions at once: the chip sends 16 bytes on P0 while the bench sends 16 on P1.

    Lenient readings: the README does not say the bench's bytes are back to back here, so each frame is
    followed by one extra idle bit (two stop bits in all). The bench's rate is 3 Mbaud x 1.02 (19.6 cycles
    per bit) and its first start bit comes `startup` cycles after reset. The README gives no edge tolerance
    for the chip's TX in this scenario, so it is judged by `uart_tx`'s rule at 20 cycles per bit: every edge
    within 2% of a whole number of bit times from its frame's start edge, and never tighter than 2 cycles.
    The chip may leave gaps between its frames.
    """

    name = "uart_3m"
    pins = {"TX": 0, "RX": 1}
    bit = 20
    startup = 1000

    def run(self, make_chip: Callable[[], Chip], image: bytes) -> Result:
        tx, rx = self.pins["TX"], self.pins["RX"]
        to_send = payload(4, 16)
        to_receive = payload(5, 16)
        sim = Sim(make_chip(), image)
        sim.send(CH_TX, to_send)
        stim = Stimulus()
        stim.at(0, rx, 1)
        end = send_uart(stim, rx, to_receive, self.startup, self.bit / 1.02, gap_bits=1)
        # Room for the chip's 16 frames three times over, in case it starts late or leaves gaps.
        run(sim, end + 3 * 10 * self.bit * len(to_send) + 2000, stim)
        sent, problems = decode_uart(sim.wave, tx, self.bit)
        difference = first_difference(to_send, sent, "host gave")
        if difference:
            problems.insert(0, "TX %s" % difference)
        difference = first_difference(to_receive, sim.received(CH_RX))
        if difference:
            problems.insert(0, "RX %s" % difference)
        if sim.contention:
            problems.append("%d pin-cycles of contention" % sim.contention)
        return Result(self.name, not problems, "; ".join(problems[:3]), sim.cycle)


BENCHES = [UartTx(), UartRx(), Uart3M()]
