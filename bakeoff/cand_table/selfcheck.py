"""Checks of this candidate that the bake-off benches do not make.

    python3 -m bakeoff.cand_table.selfcheck

1. Four different protocols run at once, one per machine, and all four are right.
2. The gated pin release of the SPI target reaches the pad three clocks after CS rises: the floor the
   pads allow (two clocks in, one out).
"""
from __future__ import annotations

import sys
from typing import List

from ..harness.chip import CLK_HZ
from ..harness.sim import Sim, payload
from .candidate import PROGRAMS, make_chip, program
from .compiler import compile_file

BIT = CLK_HZ / 115200


def uart_frames(data: bytes) -> List[int]:
    """Line levels, one per bit time."""
    levels = []
    for byte in data:
        levels += [0] + [(byte >> n) & 1 for n in range(8)] + [1]
    return levels


def decode_uart(wave: List[int], pin: int) -> bytes:
    out, i, mask = bytearray(), 1, 1 << pin
    while i < len(wave) - int(10 * BIT):
        if wave[i - 1] & mask and not wave[i] & mask:
            out.append(sum((1 if wave[i + int((1.5 + n) * BIT)] & mask else 0) << n for n in range(8)))
            i += int(9.5 * BIT)
        else:
            i += 1
    return bytes(out)


def decode_spi(wave: List[int], sck: int, mosi: int, cs: int) -> bytes:
    out, bits = bytearray(), []
    for before, now in zip(wave, wave[1:]):
        if now >> cs & 1:
            bits = []
        elif not before >> sck & 1 and now >> sck & 1:
            bits.append(now >> mosi & 1)
            if len(bits) == 8:
                out.append(int("".join(map(str, bits)), 2))
                bits = []
    return bytes(out)


def four_at_once() -> List[str]:
    to_uart, from_uart, to_spi, from_ps2 = payload(21, 6), payload(22, 6), payload(23, 6), payload(24, 4)
    sim = Sim(make_chip(), compile_file(PROGRAMS / "extra" / "four_at_once.fsm").image)
    sim.send(0, to_uart)
    sim.send(3, bytes([len(to_spi)]) + to_spi)
    uart_line = uart_frames(from_uart)
    ps2_half = 2400  # 12.5 kHz clock
    ps2_bits = []
    for byte in from_ps2:
        data = [(byte >> n) & 1 for n in range(8)]
        ps2_bits += [0] + data + [1 - sum(data) % 2, 1]
    for pin in (1, 5, 6):
        sim.set(pin, 1)
    total = max(int(len(uart_line) * BIT) + 4000, 3000 + (len(ps2_bits) + 1) * 2 * ps2_half)
    for cycle in range(total):
        index = int((cycle - 1000) / BIT)
        sim.set(1, uart_line[index] if 0 <= index < len(uart_line) and cycle >= 1000 else 1)
        slot, phase = divmod(cycle - 3000, 2 * ps2_half)
        if cycle >= 3000 and slot < len(ps2_bits):
            sim.set(6, ps2_bits[slot])       # data changes while the clock is high,
            sim.set(5, 1 if phase < ps2_half else 0)  # and is taken on the falling edge
        else:
            sim.set(5, 1)
        sim.tick()
    problems = []
    for what, want, got in (
        ("uart tx", to_uart, decode_uart(sim.wave, 0)),
        ("uart rx", from_uart, sim.received(1)),
        ("spi", to_spi, decode_spi(sim.wave, 2, 3, 4)),
        ("ps/2 rx", from_ps2, sim.received(2)),
    ):
        if want != got:
            problems.append("four at once, %s: wanted %s, got %s" % (what, want.hex(), got.hex()))
    if sim.contention:
        problems.append("four at once: %d pin-cycles of contention" % sim.contention)
    return problems


def release_latency() -> List[str]:
    """Clocks from CS rising to MISO released, for the SPI target."""
    sim = Sim(make_chip(), program("spi_target_m0"))
    sim.send(0, b"\xA5")
    for pin, level in ((0, 0), (1, 0), (3, 1)):
        sim.set(pin, level)
    sim.run(100)
    sim.set(3, 0)
    sim.run(40)
    if not sim.oe_wave[-1] & 4:
        return ["release latency: MISO is not driven while CS is low"]
    sim.set(3, 1)
    rose = sim.cycle
    sim.run(20)
    released = next(c for c in range(rose, sim.cycle) if not sim.oe_wave[c] & 4)
    return [] if released - rose == 3 else ["release latency is %d clocks, expected 3" % (released - rose)]


def main() -> int:
    problems = four_at_once() + release_latency()
    for line in problems:
        print("FAIL  " + line)
    print("selfcheck: %s" % ("%d problems" % len(problems) if problems else "ok"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
