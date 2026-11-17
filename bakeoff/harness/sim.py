"""The world around a candidate chip: pads, pull resistors, the bench's own drivers and the host link."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Tuple

from .chip import CLK_HZ, HOST_CHANNELS, HOST_CYCLES_PER_BYTE, N_PINS, Chip

MASK = (1 << N_PINS) - 1


def us(microseconds: float) -> float:
    """Microseconds as clock cycles. The result may be fractional."""
    return microseconds * CLK_HZ / 1e6


class Sim:
    """Steps one chip and resolves the level of every pin, cycle by cycle.

    Timing: the chip's `step` number n sees the pins as they were during cycle n-2, and what it returns
    reaches the pads during cycle n+1. A chip that reacts at once to a pin change shows it three cycles
    later.

    Each pin is the wired-AND of whoever drives it: the chip (when its `oe` bit is set), the bench (after
    `set`), or the pull level when nobody does. Two drivers that disagree are counted in `contention`.
    """

    def __init__(self, chip: Chip, image: bytes, pull: int = MASK) -> None:
        self.chip = chip
        chip.reset()
        chip.load(image)
        self.pull = pull & MASK
        self.cycle = 0
        self.wave: List[int] = []     # pin levels, one entry per cycle
        self.oe_wave: List[int] = []  # which pins the chip drove, one entry per cycle
        self.contention = 0           # pin-cycles where the chip and the bench drove opposite levels
        self.chip_drove_high = 0      # mask of pins the chip ever drove high (open-drain buses must stay 0)
        self.to_chip: Dict[int, Deque[int]] = {ch: deque() for ch in range(HOST_CHANNELS)}
        self.from_chip: Dict[int, List[int]] = {ch: [] for ch in range(HOST_CHANNELS)}
        self._bench_mask = 0
        self._bench_val = 0
        self._seen = (self.pull, self.pull)  # pin levels two cycles ago and one cycle ago
        self._out = 0                        # the pad output registers
        self._oe = 0

    # The bench's side of the pins.

    def set(self, pin: int, level: int) -> None:
        """The bench drives `pin` to `level` from the next tick on."""
        bit = 1 << pin
        self._bench_mask |= bit
        self._bench_val = (self._bench_val | bit) if level else (self._bench_val & ~bit)

    def release(self, pin: int) -> None:
        """The bench stops driving `pin`."""
        self._bench_mask &= ~(1 << pin)

    # The host's side of the FIFOs.

    def send(self, ch: int, data: bytes) -> None:
        """Queue bytes for host -> chip channel `ch`. They move at the host link's pace."""
        self.to_chip[ch].extend(data)

    def received(self, ch: int) -> bytes:
        """Everything the host has read from chip -> host channel `ch` so far."""
        return bytes(self.from_chip[ch])

    # Time.

    def tick(self) -> int:
        """Advance one clock cycle and return the pin levels during it."""
        out, oe = self.chip.step(self._seen[0])

        chip_oe, chip_out = self._oe, self._out
        bench_mask, bench_val = self._bench_mask, self._bench_val
        clash = chip_oe & bench_mask & (chip_out ^ bench_val)
        if clash:
            self.contention += bin(clash).count("1")
        self.chip_drove_high |= chip_oe & chip_out
        pulled_low = (chip_oe & ~chip_out) | (bench_mask & ~bench_val)
        driven = chip_oe | bench_mask
        line = ((chip_oe & chip_out) | (bench_mask & bench_val) | (~driven & self.pull)) & ~pulled_low & MASK

        self.wave.append(line)
        self.oe_wave.append(chip_oe)
        self._seen = (self._seen[1], line)
        self._out, self._oe = out & MASK, oe & MASK
        self.cycle += 1
        if self.cycle % HOST_CYCLES_PER_BYTE == 0:
            self._host()
        return line

    def peek(self) -> int:
        """The pin levels the next `tick` will show, given the bench's drivers as they are now.

        It changes nothing. A bench model that must answer a pin with no delay calls it before a tick,
        then calls `set` or `release` on its own pins, which take effect in that same cycle.
        """
        chip_oe, chip_out = self._oe, self._out
        bench_mask, bench_val = self._bench_mask, self._bench_val
        pulled_low = (chip_oe & ~chip_out) | (bench_mask & ~bench_val)
        driven = chip_oe | bench_mask
        return ((chip_oe & chip_out) | (bench_mask & bench_val) | (~driven & self.pull)) & ~pulled_low & MASK

    def run(self, n_cycles: float) -> None:
        for _ in range(int(n_cycles)):
            self.tick()

    def run_until(self, done: Callable[["Sim"], bool], limit: float) -> bool:
        """Tick until `done(sim)` is true. Return False if `limit` cycles pass first."""
        for _ in range(int(limit)):
            if done(self):
                return True
            self.tick()
        return done(self)

    def _host(self) -> None:
        for ch, queue in self.to_chip.items():
            if queue and self.chip.host_can_push(ch):
                self.chip.host_push(ch, queue.popleft())
        for ch in range(HOST_CHANNELS):
            byte = self.chip.host_pop(ch)
            if byte is not None:
                self.from_chip[ch].append(byte & 0xFF)


def edges(wave: List[int], pin: int) -> List[Tuple[int, int]]:
    """Every change of `pin` as (cycle, new level). The first entry is cycle 0 with the starting level."""
    bit = 1 << pin
    out: List[Tuple[int, int]] = []
    last = -1
    for cycle, line in enumerate(wave):
        level = 1 if line & bit else 0
        if level != last:
            out.append((cycle, level))
            last = level
    return out


@dataclass
class Result:
    bench: str
    passed: bool
    detail: str = ""
    cycles: int = 0


class Bench:
    """One scenario from bakeoff/README.md. `run` builds a Sim, plays the scenario and judges the chip."""

    name = "unnamed"
    pins: Dict[str, int] = {}

    def run(self, make_chip: Callable[[], Chip], image: bytes) -> Result:
        raise NotImplementedError


def payload(seed: int, n: int) -> bytes:
    """A fixed pseudo-random payload, so every candidate gets the same bytes."""
    state = (seed * 2654435761 + 1) & 0xFFFFFFFF
    out = bytearray()
    for _ in range(n):
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        out.append(state >> 24)
    return bytes(out)
