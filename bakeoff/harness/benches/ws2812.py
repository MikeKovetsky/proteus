"""WS2812 scenario. See bakeoff/README.md for what it asks of the chip."""
from __future__ import annotations

from typing import Callable, List, Tuple

from ..chip import CH_TX, Chip
from ..sim import MASK, Bench, Result, Sim, edges, payload
from ..stim import bytes_msb_first, first_difference, first_of, run_until

T0H, T0L, T1H, T1L, SLACK = 24, 51, 48, 27, 9
LATCH = 3000


def decode_ws2812(wave: List[int], pin: int) -> Tuple[List[int], List[str]]:
    """Decode every high pulse on `pin` as a bit, and judge each high and low time."""
    problems: List[str] = []
    changes = edges(wave, pin)
    if changes and changes[0][1]:
        problems.append("the line is high at reset, and it idles low")  # cycle 0 then counts as a rise
    else:
        changes = changes[1:]  # drop the entry for cycle 0
    bits: List[int] = []
    highs: List[str] = []
    lows: List[str] = []
    for i in range(0, len(changes), 2):
        rise = changes[i][0]
        if i + 1 >= len(changes):
            problems.append("the line is still high at the end (it rose at cycle %d)" % rise)
            break
        fall = changes[i + 1][0]
        high = fall - rise
        index = len(bits)
        if abs(high - T0H) <= SLACK:
            bit = 0
        elif abs(high - T1H) <= SLACK:
            bit = 1
        else:
            bit = 1 if high > (T0H + T1H) // 2 else 0
            highs.append(
                "bit %d is high for %d cycles at cycle %d (a 0 is %d to %d, a 1 is %d to %d)"
                % (index, high, rise, T0H - SLACK, T0H + SLACK, T1H - SLACK, T1H + SLACK)
            )
        bits.append(bit)
        want_low = T1L if bit else T0L
        if i + 2 < len(changes):
            low = changes[i + 2][0] - fall
            if low >= LATCH:
                problems.append(
                    "the line rises again at cycle %d, %d cycles after bit %d: the LEDs have latched, and only "
                    "one frame was asked for" % (changes[i + 2][0], low, index)
                )
            elif abs(low - want_low) > SLACK:
                lows.append(
                    "bit %d (a %d) is low for %d cycles at cycle %d (%d to %d allowed)"
                    % (index, bit, low, fall, want_low - SLACK, want_low + SLACK)
                )
        elif len(wave) - fall < LATCH:
            problems.append(
                "the line is low for only %d cycles after the last bit, needs %d" % (len(wave) - fall, LATCH)
            )
    return bits, problems + first_of(highs) + first_of(lows)


class Ws2812Tx(Bench):
    """The chip sends `[n][3n bytes]` as WS2812 bits. Two runs with the same image: 8 pixels, then 2.

    Lenient readings, where the README is silent:
    1. P0 has a pull-down in this bench, so the line idles low before the chip drives it.
    2. The chip may start whenever it likes within `patience` cycles of reset.
    3. Once the line has been low for 3,100 cycles after a pulse, the bench watches 3,000 more. A pulse
       after a latch-length low, in the middle of the data or after it, fails: the LEDs take what came
       before it as the whole frame.

    The README gives every high and low time with a tolerance of 9 cycles, so there is no extra slack
    between bytes or pixels: the low time after a byte's last bit is judged like any other.
    """

    name = "ws2812_tx"
    pins = {"DATA": 0}
    patience = 100000

    def run(self, make_chip: Callable[[], Chip], image: bytes) -> Result:
        pin = self.pins["DATA"]
        mask = 1 << pin
        problems: List[str] = []
        cycles = 0
        for label, n, seed in (("8 pixels", 8, 30), ("2 pixels", 2, 31)):
            data = payload(seed, 3 * n)
            sim = Sim(make_chip(), image, pull=MASK & ~mask)
            sim.send(CH_TX, bytes([n]) + data)
            last_high = [-1]

            def latched(s: Sim) -> bool:
                if s.wave and s.wave[-1] & mask:
                    last_high[0] = s.cycle - 1
                return last_high[0] >= 0 and s.cycle - 1 - last_high[0] >= LATCH + 100

            run_until(sim, latched, self.patience)
            sim.run(LATCH)  # long enough to see a second frame that should not be there
            cycles += sim.cycle
            bits, found = decode_ws2812(sim.wave, pin)
            if not bits:
                found.insert(0, "no pulse on the line in %d cycles" % sim.cycle)
            else:
                difference = first_difference(data, bytes_msb_first(bits), "host gave")
                if difference:
                    found.insert(0, difference)
                elif len(bits) != 8 * len(data):
                    found.insert(0, "sent %d bits, expected %d" % (len(bits), 8 * len(data)))
            if sim.contention:
                found.append("%d pin-cycles of contention" % sim.contention)
            problems += ["%s: %s" % (label, problem) for problem in found]
        return Result(self.name, not problems, "; ".join(problems[:3]), cycles)


BENCHES = [Ws2812Tx()]
