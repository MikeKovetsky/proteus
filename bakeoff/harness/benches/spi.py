"""SPI scenarios. See bakeoff/README.md for what each one asks of the chip."""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from ..chip import CH_RX, CH_TX, Chip
from ..sim import MASK, Bench, Result, Sim, payload
from ..stim import Stimulus, bits_msb_first, bytes_msb_first, first_difference, first_of, run, run_until

PINS = {"SCK": 0, "MOSI": 1, "MISO": 2, "CS": 3}


class SpiTarget:
    """The bench's SPI target for the controller scenarios. It answers in the same cycle (no delay).

    Mode 0: the first bit is on MISO from the cycle CS falls, and each falling SCK edge brings the next.
    Mode 3: each falling SCK edge while CS is low brings a bit, the first one included.
    MISO is released (and so pulled high) while CS is high, and after the last bit.
    """

    def __init__(self, pins: Dict[str, int], mode: int, response: bytes) -> None:
        self.sck, self.miso, self.cs = pins["SCK"], pins["MISO"], pins["CS"]
        self.mode = mode
        self.bits = bits_msb_first(response)
        self.index = 0
        self.last: Optional[Tuple[int, int]] = None
        self.selected = False  # CS has gone low
        self.finished = False  # CS has gone low and come back up

    def _put(self, sim: Sim) -> None:
        if self.index < len(self.bits):
            sim.set(self.miso, self.bits[self.index])
        else:
            sim.release(self.miso)

    def __call__(self, sim: Sim) -> None:
        line = sim.peek()
        cs = (line >> self.cs) & 1
        sck = (line >> self.sck) & 1
        last_cs, last_sck = self.last if self.last else (1, sck)
        self.last = (cs, sck)
        if cs:
            sim.release(self.miso)
            if not last_cs:
                self.finished = True
            return
        self.selected = True
        fell = bool(last_sck and not sck)
        if self.mode == 0:
            if last_cs:
                self.index = 0
                self._put(sim)
            elif fell:
                self.index += 1
                self._put(sim)
        else:
            if last_cs:
                self.index = 0
            if fell:
                self._put(sim)
                self.index += 1


def check_spi_controller(
    wave: List[int], oe_wave: List[int], pins: Dict[str, int], mode: int, n: int
) -> Tuple[bytes, List[str]]:
    """Judge a controller's waveform. Return the bytes it put on MOSI and what is wrong with the timing."""
    sck, mosi, cs = 1 << pins["SCK"], 1 << pins["MOSI"], 1 << pins["CS"]
    problems: List[str] = []
    falls = [c for c in range(1, len(wave)) if wave[c - 1] & cs and not wave[c] & cs]
    if not falls:
        return b"", ["CS never went low"]
    if len(falls) > 1:
        problems.append("CS went low %d times, expected once" % len(falls))
    start = falls[0]
    end = next((c for c in range(start, len(wave)) if wave[c] & cs), None)
    if end is None:
        problems.append("CS is still low at the end")
        end = len(wave)
    if mode == 0 and wave[start] & sck:
        problems.append("SCK is high when CS falls at cycle %d (mode 0 idles low)" % start)
    if mode == 3 and not wave[start - 1] & sck:
        problems.append("SCK is low when CS falls at cycle %d (mode 3 idles high)" % start)
    # The idle level, wherever the chip is the one holding SCK: just before CS falls, and at the very end.
    for c in (start - 1, len(wave) - 1):
        if oe_wave[c] & sck and bool(wave[c] & sck) != (mode == 3):
            problems.append(
                "chip holds SCK %s while idle at cycle %d (mode %d idles %s)"
                % (("low", "high")[mode == 0], c, mode, ("low", "high")[mode == 3])
            )
            break

    clock = [c for c in range(start, end) if not wave[c - 1] & sck and wave[c] & sck]
    if clock and clock[0] - start < 6:
        problems.append(
            "CS falls %d cycles before the first rising SCK edge at cycle %d, needs 6" % (clock[0] - start, clock[0])
        )
    if len(clock) != 8 * n:
        problems.append("%d rising SCK edges while CS is low, expected %d" % (len(clock), 8 * n))
    periods: List[str] = []
    for i in range(1, len(clock)):
        period = clock[i] - clock[i - 1]
        # Inside a byte the period is 12 give or take one. Between bytes the chip may pause.
        if (i % 8 and not 11 <= period <= 13) or period < 11:
            periods.append("SCK period is %d cycles at cycle %d (11 to 13 allowed)" % (period, clock[i]))
    problems += first_of(periods)
    moved = ["MOSI changes on the rising SCK edge at cycle %d" % c for c in clock if (wave[c] ^ wave[c - 1]) & mosi]
    problems += first_of(moved)
    return bytes_msb_first([1 if wave[c - 1] & mosi else 0 for c in clock]), problems


class SpiController(Bench):
    """The chip is the controller. Two runs with the same image: 16 bytes, then 3 bytes.

    Lenient readings, where the README is silent:
    1. The period of 12 give or take one is measured between rising edges inside a byte. Between bytes the
       chip may pause for as long as it likes. The duty cycle is not judged.
    2. The target samples MOSI in the cycle before SCK rises, and MOSI must not change in the very cycle of
       the rising edge. No other setup or hold time is asked for.
    3. In mode 0 SCK must be low in the cycle CS falls. In mode 3 SCK must be high in the cycle before CS
       falls, and CS must still be low 6 cycles before the first rising edge (the README's words), so the
       first falling edge may come as early as CS itself.
    4. SCK, MOSI and CS are pulled high until the chip drives them, and edges while CS is high are ignored.
       The idle level is judged only where the chip itself holds SCK: in the cycle before CS falls and in
       the last cycle of the run.
    5. The chip may start whenever it likes within `patience` cycles of reset. After the transfer the
       bench watches `quiet` more cycles, so a second fall of CS shows up: one transfer was asked for.
    """

    mode = 0
    pins = PINS
    patience = 50000
    quiet = 2000
    runs = (("16 bytes", 16, 10), ("3 bytes", 3, 12))

    def run(self, make_chip: Callable[[], Chip], image: bytes) -> Result:
        problems: List[str] = []
        cycles = 0
        for label, n, seed in self.runs:
            to_target = payload(seed, n)
            from_target = payload(seed + 1, n)
            sim = Sim(make_chip(), image)
            sim.send(CH_TX, bytes([n]) + to_target)
            target = SpiTarget(self.pins, self.mode, from_target)
            run_until(sim, lambda s: target.finished and len(s.from_chip[CH_RX]) >= n, self.patience, target)
            run(sim, self.quiet, target)
            cycles += sim.cycle
            found: List[str] = []
            on_mosi, timing = check_spi_controller(sim.wave, sim.oe_wave, self.pins, self.mode, n)
            if not target.selected:
                found += timing  # CS never fell, and nothing else is worth saying
            else:
                difference = first_difference(to_target, on_mosi, "host gave")
                if difference:
                    found.append("MOSI %s" % difference)
                difference = first_difference(from_target, sim.received(CH_RX), "target sent")
                if difference:
                    found.append("channel 1 %s" % difference)
                found += timing
            if sim.contention:
                found.append("%d pin-cycles of contention" % sim.contention)
            problems += ["%s: %s" % (label, problem) for problem in found]
        return Result(self.name, not problems, "; ".join(problems[:3]), cycles)


class SpiControllerMode0(SpiController):
    name = "spi_ctrl_m0"
    mode = 0


class SpiControllerMode3(SpiController):
    name = "spi_ctrl_m3"
    mode = 3
    runs = (("16 bytes", 16, 14), ("3 bytes", 3, 16))


class SpiTargetMode0(Bench):
    """The chip is the target in mode 0. The bench clocks 8 bytes at 2 MHz, back to back.

    Lenient readings, where the README is silent:
    1. CS falls at cycle `select_at`, long after the host has offered the 8 bytes (the first can arrive at
       cycle 16 and the last at cycle 128, if the chip takes them).
    2. "Stable from 3 cycles before the rising edge to 1 cycle after it" is the four cycles r-3 to r, where
       r is the first cycle SCK is high. Only the level on the pad is judged, not who drives it.
    3. The chip must not drive MISO before CS falls, and gets `release_slack` cycles to let go after CS
       rises (the pads alone cost 3).
    4. SCK and MOSI have pull-downs in this bench, so they are low from the chip's first clock. The bench
       drives MOSI from the falling SCK edge (from CS falling for the first bit), raises CS 30
       cycles after the last falling SCK edge, and waits `patience` cycles for the bytes on channel 1.
    """

    name = "spi_target_m0"
    pins = PINS
    select_at = 3000
    period = 30
    release_slack = 8
    patience = 20000

    def run(self, make_chip: Callable[[], Chip], image: bytes) -> Result:
        sck, mosi, miso, cs = (self.pins[k] for k in ("SCK", "MOSI", "MISO", "CS"))
        to_return = payload(18, 8)
        to_chip = payload(19, 8)
        # Pull-downs on SCK and MOSI, so the chip sees their idle levels from its very first clock.
        sim = Sim(make_chip(), image, pull=MASK & ~((1 << sck) | (1 << mosi)))
        sim.send(CH_TX, to_return)
        stim = Stimulus()
        stim.at(0, sck, 0)
        stim.at(0, mosi, 0)
        stim.at(0, cs, 1)
        stim.at(self.select_at, cs, 0)
        half = self.period // 2
        rising: List[int] = []
        for k, bit in enumerate(bits_msb_first(to_chip)):
            rise = self.select_at + 60 + k * self.period
            stim.at(self.select_at if k == 0 else rise - half, mosi, bit)
            stim.at(rise, sck, 1)
            stim.at(rise + half, sck, 0)
            rising.append(rise)
        deselect_at = rising[-1] + half + self.period
        stim.at(deselect_at, cs, 1)
        run_until(
            sim,
            lambda s: s.cycle > deselect_at and len(s.from_chip[CH_RX]) >= len(to_chip),
            deselect_at + self.patience,
            stim,
        )
        run(sim, 200, stim)

        problems: List[str] = []
        wave, mask = sim.wave, 1 << miso
        want_bits = bits_msb_first(to_return)
        on_miso = bytes_msb_first([1 if wave[r - 1] & mask else 0 for r in rising])
        difference = first_difference(to_return, on_miso, "host gave")
        if difference:
            problems.append("MISO %s" % difference)
        difference = first_difference(to_chip, sim.received(CH_RX))
        if difference:
            problems.append("channel 1 %s" % difference)
        unstable: List[str] = []
        for k, rise in enumerate(rising):
            window = [1 if wave[c] & mask else 0 for c in range(rise - 3, rise + 1)]
            if any(level != want_bits[k] for level in window):
                unstable.append(
                    "MISO is %s over cycles %d to %d (SCK rises at %d), expected %d throughout for byte %d bit %d"
                    % ("".join(map(str, window)), rise - 3, rise, rise, want_bits[k], k // 8, 7 - k % 8)
                )
        problems += first_of(unstable)
        early = [c for c in range(self.select_at) if sim.oe_wave[c] & mask]
        if early:
            problems.append("chip drives MISO at cycle %d, before CS falls at %d" % (early[0], self.select_at))
        late = [c for c in range(deselect_at + self.release_slack, len(wave)) if sim.oe_wave[c] & mask]
        if late:
            problems.append(
                "chip still drives MISO at cycle %d, %d cycles after CS rose" % (late[0], late[0] - deselect_at)
            )
        if sim.contention:
            problems.append("%d pin-cycles of contention" % sim.contention)
        return Result(self.name, not problems, "; ".join(problems[:3]), sim.cycle)


BENCHES = [SpiControllerMode0(), SpiControllerMode3(), SpiTargetMode0()]
