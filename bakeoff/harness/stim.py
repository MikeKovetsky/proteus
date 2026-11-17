"""Helpers the benches share: scheduled pin changes on a floating-point time base, and the loops that play them.

An "actor" is anything callable as `actor(sim)`. The loops below call every actor once before each tick, so
an actor may `set` or `release` bench pins for the cycle that is about to run. `Stimulus` is an actor that
plays a fixed schedule. The protocol modules define reactive actors (an SPI target, an I2C target, a 1-Wire
device) that look at `sim.peek()` and answer in the same cycle.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from .sim import Sim

Actor = Callable[[Sim], None]


class Stimulus:
    """Pin changes at fractional cycle times, sampled onto the clock grid.

    A change scheduled at time t shows on the pads from the first whole cycle at or after t, which is what
    a pad sampled at 60 MHz would see of an ideal waveform. Build one Stimulus per Sim.
    """

    def __init__(self) -> None:
        self._events: List[Tuple[float, int, int, Optional[int]]] = []
        self._next = 0
        self._sorted = True
        self.end = 0.0  # the time of the last scheduled change

    def at(self, time: float, pin: int, level: Optional[int]) -> None:
        """At `time` drive `pin` to `level`, or stop driving it when `level` is None."""
        self._events.append((time, len(self._events), pin, level))
        self._sorted = False
        if time > self.end:
            self.end = time

    def __call__(self, sim: Sim) -> None:
        if not self._sorted:
            done = self._events[: self._next]
            rest = sorted(self._events[self._next :])
            self._events = done + rest
            self._sorted = True
        events = self._events
        while self._next < len(events) and events[self._next][0] <= sim.cycle:
            _, _, pin, level = events[self._next]
            if level is None:
                sim.release(pin)
            else:
                sim.set(pin, level)
            self._next += 1


def run(sim: Sim, n_cycles: float, *actors: Actor) -> None:
    """Tick `n_cycles` times, calling every actor before each tick."""
    for _ in range(int(n_cycles)):
        for actor in actors:
            actor(sim)
        sim.tick()


def run_until(sim: Sim, done: Callable[[Sim], bool], limit: float, *actors: Actor) -> bool:
    """Tick until `done(sim)` is true, calling every actor before each tick. False if `limit` cycles pass first."""
    for _ in range(int(limit)):
        if done(sim):
            return True
        for actor in actors:
            actor(sim)
        sim.tick()
    return done(sim)


def first_difference(want: bytes, got: bytes, verb: str = "sent") -> Optional[str]:
    """Describe the first place `got` departs from `want`, or return None when they are equal."""
    for index, (w, g) in enumerate(zip(want, got)):
        if w != g:
            return "byte %d: %s %02x, got %02x" % (index, verb, w, g)
    if len(want) != len(got):
        return "got %d bytes, expected %d" % (len(got), len(want))
    return None


def low_runs(levels: List[int]) -> List[Tuple[int, int]]:
    """Every run of zeros in `levels` as (first cycle, length)."""
    runs: List[Tuple[int, int]] = []
    start = -1
    for cycle, level in enumerate(levels):
        if not level and start < 0:
            start = cycle
        elif level and start >= 0:
            runs.append((start, cycle - start))
            start = -1
    if start >= 0:
        runs.append((start, len(levels) - start))
    return runs


def first_of(problems: List[str]) -> List[str]:
    """Keep the first of a list of similar problems, and say how many more there were."""
    if len(problems) < 2:
        return list(problems)
    return ["%s (and %d more like it)" % (problems[0], len(problems) - 1)]


def bits_msb_first(data: bytes) -> List[int]:
    return [(byte >> b) & 1 for byte in data for b in range(7, -1, -1)]


def bits_lsb_first(data: bytes) -> List[int]:
    return [(byte >> b) & 1 for byte in data for b in range(8)]


def bytes_msb_first(bits: List[int]) -> bytes:
    """Pack bits into bytes, first bit highest. Bits past the last whole byte are dropped."""
    return bytes(sum(bit << (7 - k) for k, bit in enumerate(bits[i : i + 8])) for i in range(0, len(bits) - 7, 8))


def bytes_lsb_first(bits: List[int]) -> bytes:
    """Pack bits into bytes, first bit lowest. Bits past the last whole byte are dropped."""
    return bytes(sum(bit << k for k, bit in enumerate(bits[i : i + 8])) for i in range(0, len(bits) - 7, 8))
