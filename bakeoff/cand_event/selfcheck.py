"""Checks on the candidate that the bake-off benches do not make.

    python3 -m bakeoff.cand_event.selfcheck

1. Both lanes at once: lane 0 runs a full-duplex 3 Mbaud UART while lane 1 runs an SPI controller whose
   MOSI is looped back to MISO. Three byte streams cross the chip at the same time and all must match.
2. The budget table adds up and stays inside the bake-off's limits.
"""
from __future__ import annotations

import sys
from typing import List

from ..harness.benches.uart import decode_uart
from ..harness.sim import Sim, payload
from . import candidate, machine
from .asm import assemble


def two_lanes() -> List[str]:
    image = assemble((candidate.PROGRAMS / "demo_two_lanes.s").read_text())
    sim = Sim(candidate.make_chip(), image)
    uart_out, uart_in, spi = payload(11, 24), payload(12, 24), payload(13, 16)
    sim.send(0, uart_out)
    sim.send(3, bytes([len(spi)]) + spi)
    bit = 20
    levels: List[int] = [1] * 200           # the bench's UART sender on P1: idle, then 24 frames
    for byte in uart_in:
        for value in [0] + [(byte >> i) & 1 for i in range(8)] + [1, 1]:
            levels += [value] * bit
    for cycle in range(len(levels) + 3000):
        sim.set(1, levels[cycle] if cycle < len(levels) else 1)
        sim.set(6, (sim.wave[-1] >> 5) & 1 if sim.wave else 1)   # MISO follows MOSI
        sim.tick()
    problems: List[str] = []
    sent, edge_problems = decode_uart(sim.wave, 0, bit)
    if sent != uart_out or edge_problems:
        problems.append("lane 0 UART TX: %s %s" % (sent.hex(), edge_problems[:1]))
    if sim.received(1) != uart_in:
        problems.append("lane 0 UART RX: got %s" % sim.received(1).hex())
    if sim.received(2) != spi:
        problems.append("lane 1 SPI loopback: got %s" % sim.received(2).hex())
    if sim.contention:
        problems.append("%d pin-cycles of contention" % sim.contention)
    return problems


def budget() -> List[str]:
    b = candidate.make_chip().budget()
    lane = sum(machine.LANE_FLOPS.values())
    problems = []
    if b["flops"] != 2 * lane + sum(machine.SHARED_FLOPS.values()):
        problems.append("flop table does not add up")
    if b["flops"] > 3000 or b["sram_bits"] > 32768:
        problems.append("over budget: %s" % b)
    return problems


def main() -> int:
    failed = 0
    for label, check in (("two protocols at once on two lanes", two_lanes), ("budget adds up", budget)):
        problems = check()
        print("%s  %s%s" % ("FAIL" if problems else "ok  ", label, "  (%s)" % "; ".join(problems) if problems else ""))
        failed += bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
