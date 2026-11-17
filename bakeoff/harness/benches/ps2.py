"""PS/2 scenario. See bakeoff/README.md for what it asks of the chip."""
from __future__ import annotations

from typing import Callable, List

from ..chip import CH_RX, CH_STATUS, Chip
from ..sim import Bench, Result, Sim, payload
from ..stim import Stimulus, first_difference, run, run_until


def ps2_frame_bits(byte: int, good_parity: bool = True) -> List[int]:
    """Start (0), 8 data bits LSB first, odd parity, stop (1)."""
    data = [(byte >> b) & 1 for b in range(8)]
    parity = 1 - sum(data) % 2  # odd parity: the nine bits together hold an odd number of ones
    return [0] + data + [parity if good_parity else 1 - parity] + [1]


class Ps2Rx(Bench):
    """The bench plays a PS/2 device: 8 frames at 12.5 kHz, the sixth with wrong parity.

    Lenient readings, where the README is silent:
    1. Both lines idle high for `startup` cycles before the first frame, and for two clock periods
       (9,600 cycles) between frames.
    2. DATA changes in the same cycle CLK rises, and CLK falls half a period (2,400 cycles) later. So each
       bit is valid from 2,400 cycles before its falling edge until the next rising edge, and a chip that
       samples at the falling edge, or anywhere in the low half, reads it right.
    3. The bad frame has a good start and stop bit. Its byte must not reach channel 1.
    4. The chip may take up to `patience` cycles after the last frame to deliver.

    "The chip only listens" is taken at its word: driving CLK or DATA at all fails the bench.
    """

    name = "ps2_rx"
    pins = {"CLK": 0, "DATA": 1}
    period = 4800
    startup = 5000
    bad_frame = 5
    patience = 20000

    def run(self, make_chip: Callable[[], Chip], image: bytes) -> Result:
        clk, data_pin = self.pins["CLK"], self.pins["DATA"]
        data = payload(40, 8)
        good = bytes(byte for i, byte in enumerate(data) if i != self.bad_frame)
        sim = Sim(make_chip(), image)
        stim = Stimulus()
        stim.at(0, clk, 1)
        stim.at(0, data_pin, 1)
        t = float(self.startup)
        for i, byte in enumerate(data):
            for k, bit in enumerate(ps2_frame_bits(byte, i != self.bad_frame)):
                stim.at(t + k * self.period, data_pin, bit)
                stim.at(t + (k + 0.5) * self.period, clk, 0)
                stim.at(t + (k + 1) * self.period, clk, 1)
            t += (11 + 2) * self.period
        run_until(
            sim,
            lambda s: s.cycle >= t and len(s.from_chip[CH_RX]) >= len(good) and len(s.from_chip[CH_STATUS]) >= 1,
            t + self.patience,
            stim,
        )
        run(sim, 2000, stim)

        problems: List[str] = []
        got = sim.received(CH_RX)
        difference = first_difference(good, got)
        if difference:
            kept = got[: self.bad_frame + 1] == data[: self.bad_frame + 1]
            hint = " (that is the byte of frame %d, which has wrong parity)" % self.bad_frame if kept else ""
            problems.append("channel 1 %s%s" % (difference, hint))
        difference = first_difference(b"\xff", sim.received(CH_STATUS), "expected")
        if difference:
            problems.append("channel 2 %s" % difference)
        both = (1 << clk) | (1 << data_pin)
        drove = next((c for c, oe in enumerate(sim.oe_wave) if oe & both), None)
        if drove is not None:
            problems.append("chip drives CLK or DATA at cycle %d, and it must only listen" % drove)
        if sim.contention:
            problems.append("%d pin-cycles of contention" % sim.contention)
        return Result(self.name, not problems, "; ".join(problems[:3]), sim.cycle)


BENCHES = [Ps2Rx()]
