"""Checks on the candidate that the bake-off benches do not make.

    python3 -m bakeoff.cand_event.selfcheck

1. Both lanes at once: lane 0 runs a full-duplex 3 Mbaud UART while lane 1 runs an SPI controller whose
   MOSI is looped back to MISO. Three byte streams cross the chip at the same time and all must match.
2. An `act` strobe goes through the strobe register, so its helper acts one cycle after the instruction.
3. The assembler only does arithmetic on the numbers a program writes, and a label defined twice is an error.
4. The limits README.md gives: the SPI target's top clock, and the one sample phase Ethernet receive works at.
5. The two receivers beyond the benches: eth10_rx over clock phase, +-200 ppm and frame size, and usb_ls_rx
   over phase, +-1.5% and payload, with the benches' own signal generators.
6. README.md's budget tables say what machine.py counts, and stay inside the bake-off's limits.
"""
from __future__ import annotations

import random
import re
import sys
from typing import Dict, List

from ..harness.benches.eth10 import Eth10Rx, send_eth_frame
from ..harness.benches.spi import SpiTargetMode0
from ..harness.benches.uart import decode_uart
from ..harness.benches.usb_ls import send_usb_packet, with_crc
from ..harness.codec import crc32_eth
from ..harness.sim import MASK, Sim, payload
from ..harness.stim import Stimulus, run
from . import candidate, machine
from .asm import AsmError, assemble, assemble_lanes


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


def strobe_latency() -> List[str]:
    """`pin p1=0` fires at T and P1 falls on the pad at T+1. `act tx.tick` fires at T+2, the strobe register
    holds it for a cycle, the shifter puts out its 1 at T+3, and P0 rises on the pad at T+4."""
    sim = Sim(candidate.make_chip(), assemble(
        "cfg tx nbits=8 lsb ext\ncfg pins p0=tx\npin p0=0, p1=1\n#1 -> tx\npin p1=0\nact tx.tick\nhalt\n"))
    sim.run(40)
    fall = next(c for c in range(1, len(sim.wave)) if sim.wave[c - 1] & 2 and not sim.wave[c] & 2)
    rise = next((c for c in range(fall, len(sim.wave)) if sim.wave[c] & 1), len(sim.wave))
    if rise - fall != 3:
        return ["act tx.tick moves P0 %d cycles after pin p1=0 moves P1, expected 3" % (rise - fall)]
    return []


def numbers_only() -> List[str]:
    """Constants are arithmetic. Anything else in an expression must stop the assembler, and not run."""
    problems = []
    if assemble("#2 * (3 + 4) - 10 // 3 -> r0") != assemble("#11 -> r0"):
        problems.append("assembler: 2 * (3 + 4) - 10 // 3 does not come out as 11")
    for expr in ("().__len__()", "().__class__.__base__.__subclasses__().__len__()", "[7][0]"):
        try:
            assemble("#%s -> r0" % expr)
            problems.append("assembler: `%s` assembled, and only arithmetic should" % expr)
        except AsmError:
            pass
    try:
        assemble("again: halt\nagain: halt\n")
        problems.append("assembler: a label defined twice assembled")
    except AsmError:
        pass
    return problems


def limits() -> List[str]:
    """README.md, limits 1 and 2. The SPI target passes at 14 cycles per SCK period (4.3 MHz) and fails at 13
    (4.6 MHz). Ethernet receive passes with the sampler's phase at 4 and fails at 3 and at 5."""
    problems = []
    spi: Dict[int, bool] = {}
    for period in (14, 13):
        bench = SpiTargetMode0()
        bench.period = period
        spi[period] = bench.run(candidate.make_chip, candidate.program("spi_target_m0")).passed
    if spi != {14: True, 13: False}:
        problems.append("spi_target_m0 passes at these SCK periods: %s, README says 14 and not 13" % spi)
    source = (candidate.PROGRAMS / "eth10_rx.s").read_text()
    if "#4 -> smp.phase" not in source:
        return problems + ["eth10_rx.s no longer sets the sampler's phase with `#4 -> smp.phase`"]
    eth = {phase: Eth10Rx().run(candidate.make_chip, assemble(
        source.replace("#4 -> smp.phase", "#%d -> smp.phase" % phase))).passed for phase in (3, 4, 5)}
    if eth != {3: False, 4: True, 5: False}:
        problems.append("eth10_rx passes at these sample phases: %s, README says only 4" % eth)
    return problems


def eth_once(image: bytes, frame: bytes, ppm: float, phase: float, flip: bool = False) -> bool:
    """One frame and its FCS on P2, as the eth10_rx bench sends it, `phase` cycles late. Did the chip get it?"""
    sent = bytearray(frame + crc32_eth(frame).to_bytes(4, "little"))
    if flip:
        sent[20] ^= 0x04
    sim = Sim(candidate.make_chip(), image, pull=MASK & ~4)
    stim = Stimulus()
    stim.at(0, 2, 0)
    end = send_eth_frame(stim, 2, b"\x55" * 7 + b"\xd5" + bytes(sent), 200 + phase, 6 * (1 + ppm * 1e-6))
    run(sim, end + 600, stim)
    status = bytes([1 if flip else 0]) + len(sent).to_bytes(2, "little")
    return sim.received(1) == bytes(sent) and sim.received(2) == status


def usb_once(image: bytes, pid: int, data: bytes, bit: float, phase: float, flip: bool = False) -> bool:
    """One packet on P0 and P1, as the usb_ls_rx bench sends it, `phase` cycles late. Did the chip get it?"""
    body = bytearray(with_crc(pid, data))
    if flip:
        body[-1] ^= 0x10
    sim = Sim(candidate.make_chip(), image, pull=MASK & ~1)
    stim = Stimulus()
    end = send_usb_packet(stim, bytes(body), 300 + phase, bit)
    run(sim, end + 800, stim)
    return (sim.received(1) == bytes(body) and sim.received(2) == bytes([1 if flip else 0, len(body)])
            and not any(oe & 3 for oe in sim.oe_wave))


def sweeps() -> List[str]:
    """eth10_rx: 24 phases x 5 rates up to +-200 ppm x 3 sizes. usb_ls_rx: 16 phases x 5 rates up to +-1.5% x
    5 payloads x 2 PIDs. Every run must pass, and a flipped bit and a bad CRC must be reported."""
    problems = []
    rnd = random.Random(7)
    image = candidate.program("eth10_rx")
    runs = [(ppm, i / 8, size) for ppm in (-200, -100, 0, 100, 200) for i in range(24) for size in (60, 61, 300)]
    failed = [r for r in runs if not eth_once(image, bytes(rnd.randrange(256) for _ in range(r[2])), r[0], r[1])]
    if failed:
        problems.append("eth10_rx: %d of %d runs failed, the first at %s (ppm, phase, size)"
                        % (len(failed), len(runs), failed[0]))
    if not eth_once(image, bytes(range(64)), 200, 0.3, flip=True):
        problems.append("eth10_rx: a frame with a flipped bit is not reported as bad")
    image = candidate.program("usb_ls_rx")
    datas = [b"", bytes([0xFF] * 8), bytes(rnd.randrange(256) for _ in range(8)), bytes(5), bytes([0x7E, 0xFC, 0x3F])]
    runs = [(40 * rate, i * 2.5, d, pid) for rate in (0.985, 0.99, 1.0, 1.01, 1.015) for i in range(16)
            for d in datas for pid in (0xC3, 0x4B)]
    failed = [r for r in runs if not usb_once(image, r[3], r[2], r[0], r[1])]
    if failed:
        problems.append("usb_ls_rx: %d of %d runs failed, the first at bit %.1f, phase %.1f, data %s"
                        % (len(failed), len(runs), failed[0][0], failed[0][1], failed[0][2].hex()))
    if not usb_once(image, 0xC3, b"hello", 40, 3.0, flip=True):
        problems.append("usb_ls_rx: a packet with a bad CRC is not reported as bad")
    return problems


def budget() -> List[str]:
    """README.md's budget tables, totals, gate line and largest program say what the code counts."""
    b = candidate.make_chip().budget()
    lane, shared = sum(machine.LANE_FLOPS.values()), sum(machine.SHARED_FLOPS.values())
    lane_gates, shared_gates = sum(machine.LANE_GATES.values()), sum(machine.SHARED_GATES.values())
    text = (candidate.PROGRAMS.parent / "README.md").read_text()
    lane_part, _, shared_part = text.partition("| Shared | Flops |")
    row = r"^\| ([^|*]+) \| (\d+) \|$"
    lane_rows = [int(n) for _, n in re.findall(row, lane_part.split("## Budget")[-1], re.M)]
    shared_rows = [int(n) for _, n in re.findall(row, shared_part.split("\n\n")[0], re.M)]
    one_lane = re.search(r"\*\*One lane\*\* \| \*\*(\d+)\*\*", text)
    whole = re.search(r"\*\*Whole chip: (\d) × (\d+) \+ (\d+)\*\* \| \*\*([\d,]+)\*\*", text)
    gates = re.search(r"\(2 × ([\d,]+) per lane \+ (\d+) shared\)", text)
    largest = re.search(r"The largest program is (\d+) words \((\w+)\)", text)
    words = {path.stem: len(assemble_lanes(path.read_text())[0]) for path in candidate.PROGRAMS.glob("*.s")
             if path.stem != "demo_two_lanes"}
    biggest = max(words, key=words.get)
    problems = []
    if b["flops"] != 2 * lane + shared or b["gates_estimate"] != 2 * lane_gates + shared_gates:
        problems.append("budget() does not add up its own tables")
    if [sum(lane_rows), sum(shared_rows)] != [lane, shared]:
        problems.append("README rows add up to %d per lane and %d shared, machine.py to %d and %d"
                        % (sum(lane_rows), sum(shared_rows), lane, shared))
    if not one_lane or int(one_lane.group(1)) != lane:
        problems.append("README's lane total is not %d" % lane)
    want = [2, lane, shared, b["flops"]]
    if not whole or [int(g.replace(",", "")) for g in whole.groups()] != want:
        problems.append("README's whole-chip line is not 2 x %d + %d = %d" % tuple(want[1:]))
    if not gates or [int(g.replace(",", "")) for g in gates.groups()] != [lane_gates, shared_gates]:
        problems.append("README's gate line is not 2 x %d per lane + %d shared" % (lane_gates, shared_gates))
    if not largest or [int(largest.group(1)), largest.group(2)] != [words[biggest], biggest]:
        problems.append("README's largest program is not %s at %d words" % (biggest, words[biggest]))
    if b["flops"] > 3000 or b["sram_bits"] > 32768:
        problems.append("over budget: %s" % b)
    return problems


def main() -> int:
    failed = 0
    for label, check in (
        ("two protocols at once on two lanes", two_lanes),
        ("act strobes reach their helper one cycle after the instruction", strobe_latency),
        ("the assembler does arithmetic only, and refuses a label defined twice", numbers_only),
        ("the SPI target's top clock and the Ethernet sample phase are as README says", limits),
        ("eth10_rx 360 runs and usb_ls_rx 800 runs over phase and rate", sweeps),
        ("README budget matches machine.py", budget),
    ):
        problems = check()
        print("%s  %s%s" % ("FAIL" if problems else "ok  ", label, "  (%s)" % "; ".join(problems) if problems else ""))
        failed += bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
