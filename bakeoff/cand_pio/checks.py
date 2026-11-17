"""Checks of cand_pio beyond the benches, with its own signal generators.

    python3 -m bakeoff.cand_pio.checks

Each check holds a claim in README.md to the code, and the exit status is 1 when any claim does not hold.

1. eth10_rx over every clock phase and +-200 ppm.   2. usb_ls_rx over phases and +-1.5%.
3. Four machines at once.   4. Which programs still pass at 30 MHz (a second machine enabled on memory A).
5. Which programs still pass if a taken jump costs two clocks.   6. The assembler refuses fields that do not fit.
7. The budget table in README.md says what `budget()` counts.
"""
from __future__ import annotations

import random
import re
import zlib
from typing import List, Tuple

from ..harness.sim import MASK, Sim, payload
from ..run import discover_benches
from . import candidate as c
from . import isa
from .asm import AsmError, Assembler, assemble
from .chip import PioChip, _SM

NEEDS_60MHZ = {"eth10_tx", "eth10_rx", "usb_ls_rx"}    # README, Concurrency 2
NEEDS_ONE_CLOCK_JUMPS = {"eth10_tx", "eth10_rx"}        # README, Failing scenarios 2


def run_eth(frame: bytes, ppm: float, phase: float, flip: bool = False) -> bool:
    sent = bytearray(frame + zlib.crc32(frame).to_bytes(4, "little"))
    if flip:
        sent[20] ^= 0x04
    halves = []
    for byte in bytes([0x55] * 7 + [0xD5]) + bytes(sent):
        for i in range(8):
            halves += [1 - ((byte >> i) & 1), (byte >> i) & 1]
    sim = Sim(c.make_chip(), c.program("eth10_rx"), pull=MASK & ~4)
    half, start = 3.0 * (1 + ppm * 1e-6), 200 + phase
    end = start + len(halves) * half
    for n in range(int(end) + 600):
        sim.set(2, 0 if n < start else halves[int((n - start) / half)] if n < end else 1 if n < end + 18 else 0)
        sim.tick()
    status = bytes([1 if flip else 0, len(sent) & 255, len(sent) >> 8])
    return sim.received(1) == bytes(sent) and sim.received(2) == status


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        for i in range(8):
            crc = (crc >> 1) ^ (0xA001 if (crc ^ (byte >> i)) & 1 else 0)
    return crc ^ 0xFFFF


def run_usb(pid: int, data: bytes, rate: float, phase: float, flip: bool = False) -> bool:
    pkt = bytearray(bytes([pid]) + data + crc16(data).to_bytes(2, "little"))
    if flip:
        pkt[-1] ^= 0x10
    bits = [0] * 7 + [1] + [(byte >> i) & 1 for byte in pkt for i in range(8)]
    states, level, ones = [], "J", 0
    for b in bits:
        ones = ones + 1 if b else 0
        level = level if b else ("K" if level == "J" else "J")
        states.append(level)
        if ones == 6:
            level, ones = ("K" if level == "J" else "J"), 0
            states.append(level)
    states += ["0", "0", "J"]
    sim = Sim(c.make_chip(), c.program("usb_ls_rx"), pull=MASK & ~1)
    bit, start = 40.0 * rate, 300 + phase
    end = start + len(states) * bit
    for n in range(int(end) + 800):
        if start <= n < end:
            s = states[int((n - start) / bit)]
            sim.set(0, 1 if s == "K" else 0)
            sim.set(1, 1 if s == "J" else 0)
        else:
            sim.release(0)
            sim.release(1)
        sim.tick()
    return (sim.received(1) == bytes(pkt) and sim.received(2) == bytes([1 if flip else 0, len(pkt)])
            and not any(sim.oe_wave))


UART = """
.sm %d
.tick %s
tx%d:   pull %s
        set p%d, 0  @tick
bit%d:  out p%d, 1  @tick
        jmp !osre, bit%d
        set p%d, 1  @tick
        jmp tx%d
.sm %d
.tick %s
.autopush %s
        nop [3]
rx%d:   wait 1, p%d
        wait 0, p%d
        timer half
        set x, 7    @tick
rb%d:   in p%d, 1   @tick
        jmp x--, rb%d
        jmp rx%d
"""


def four_machines() -> bool:
    """A 115200 baud loop on memory A and a 3 Mbaud loop on memory B, P0->P1 and P2->P3 wired by this test."""
    src = ".pads out=0b0101 oe=0b0101\n"
    src += UART % (0, "520.8333", 0, "", 0, 0, 0, 0, 0, 0, 1, "520.8333", "", 1, 1, 1, 1, 1, 1, 1)
    src += UART % (2, "20", 2, "ch3", 2, 2, 2, 2, 2, 2, 3, "20", "ch2", 3, 3, 3, 3, 3, 3, 3)
    sim = Sim(c.make_chip(), assemble(src))
    a, b = payload(11, 12), payload(12, 40)
    sim.send(0, a)
    sim.send(3, b)
    line = MASK
    for _ in range(90000):
        sim.set(1, line & 1)
        sim.set(3, (line >> 2) & 1)
        line = sim.tick()
    return sim.received(1) == a and sim.received(2) == b and not sim.contention


class SlowJumpChip(PioChip):
    """cand_pio if the next-address path misses the clock: a taken jump costs the machine its next slot.

    A jump to the next word, a stall and a delay cost nothing more. A tick that falls in the lost slot is kept.
    """

    def reset(self) -> None:
        super().reset()
        self.lost = [False] * isa.N_SM
        self.held = [False] * isa.N_SM

    def _slot(self, k: int, s: _SM, ins: tuple, pins: int) -> None:
        if self.lost[k]:
            self.lost[k] = False
            self.held[k] = s.tick           # step() clears a machine's tick after every slot it gets
            return
        pc = s.pc
        super()._slot(k, s, ins, pins)
        self.lost[k] = s.pc not in (pc, (pc + 1) & 0x1FF)

    def step(self, pins: int) -> Tuple[int, int]:
        out = super().step(pins)
        for k, s in enumerate(self.sm):
            if self.held[k]:
                s.tick, self.held[k] = True, False
        return out


TOO_WIDE = (".sm 4", ".tick 0.5", ".side p0:3", ".pads oe=0x100", "set p0:7, 1", "set p0:2, 4", "dir p0:0, 0",
            "toggle p0:8", "wait 2, p0", "waitt 1, p0, 256, here", "push #0x100", "mov osr, x, 33", "nop side 4")


def assembler_refuses() -> List[str]:
    """What goes wrong with the lines of TOO_WIDE, each with a field wider than the word holds: none may assemble."""
    problems = []
    for line in TOO_WIDE:
        try:
            assemble(".sm 0\nhere: halt\n%s\n" % line)
        except AsmError:
            continue
        except Exception as error:      # a crash is not an error message
            problems.append("%s crashes it (%s)" % (line, type(error).__name__))
            continue
        problems.append("it takes %s" % line)
    return problems


def budget_against_readme() -> List[str]:
    """Where README.md's budget table and totals differ from what `budget()` counts."""
    b = c.make_chip().budget()
    per_sm, shared = sum(b["per_machine"].values()), sum(b["shared"].values())
    text = (c.PROGRAMS.parent / "README.md").read_text()
    rows = re.findall(r"^\| ([^|*]+?) \| (\d+) \| ([^|*]+?) \| (\d+) \|$", text, re.M)
    totals = re.search(r"\*\*Total per machine\*\* \| \*\*(\d+)\*\* \| \*\*Total shared\*\* \| \*\*(\d+)\*\*", text)
    flops = re.search(r"\*\*Flops: ([\d,]+)\*\* \((\d) × (\d+) \+ (\d+)\)", text)
    sram = re.search(r"\*\*SRAM: ([\d,]+) bits\*\*", text)
    largest = re.search(r"The largest program here is (\d+) words", text)
    words = max(sum(map(len, Assembler().assemble(path.read_text())[1])) for path in c.PROGRAMS.glob("*.pio"))
    problems = []
    if b["flops"] != isa.N_SM * per_sm + shared:
        problems.append("budget() says %d flops, its tables add up to %d" % (b["flops"], isa.N_SM * per_sm + shared))
    if [sum(int(r[1]) for r in rows), sum(int(r[3]) for r in rows)] != [per_sm, shared]:
        problems.append("the README's rows add up to %d and %d, budget() to %d and %d"
                        % (sum(int(r[1]) for r in rows), sum(int(r[3]) for r in rows), per_sm, shared))
    if not totals or [int(totals.group(1)), int(totals.group(2))] != [per_sm, shared]:
        problems.append("the README's totals are not %d per machine and %d shared" % (per_sm, shared))
    want = [b["flops"], isa.N_SM, per_sm, shared]
    if not flops or [int(flops.group(1).replace(",", ""))] + [int(g) for g in flops.groups()[1:]] != want:
        problems.append("the README's flop line is not %d (%d x %d + %d)" % tuple(want))
    if not sram or int(sram.group(1).replace(",", "")) != b["sram_bits"]:
        problems.append("the README's SRAM line is not %d bits" % b["sram_bits"])
    if not largest or int(largest.group(1)) != words:
        problems.append("the README's largest program is not %d words" % words)
    return problems


def main() -> int:
    failed = 0

    def report(ok: bool, text: str) -> None:
        nonlocal failed
        print("%s  %s" % ("ok  " if ok else "FAIL", text))
        failed += not ok

    rnd = random.Random(7)
    runs = [(ppm, i / 8, size) for ppm in (-200, -100, 0, 100, 200) for i in range(24) for size in (60, 61, 300)]
    bad = sum(not run_eth(bytes(rnd.randrange(256) for _ in range(size)), ppm, ph) for ppm, ph, size in runs)
    flipped = run_eth(bytes(range(64)), 200, 0.3, flip=True)
    report(not bad and flipped, "eth10_rx: %d runs over phase and ppm, %d failed; flipped bit reported: %s"
           % (len(runs), bad, flipped))
    datas = [b"", bytes([0xFF] * 8), bytes(rnd.randrange(256) for _ in range(8)), bytes(5), bytes([0x7E, 0xFC, 0x3F])]
    runs = [(r, i * 2.5, d, p) for r in (0.985, 0.99, 1.0, 1.01, 1.015) for i in range(16) for d in datas
            for p in (0xC3, 0x4B)]
    bad = sum(not run_usb(p, d, r, ph) for r, ph, d, p in runs)
    bad_crc = run_usb(0xC3, b"hello", 1.0, 3.0, flip=True)
    report(not bad and bad_crc, "usb_ls_rx: %d runs over phase and rate, %d failed; bad CRC reported: %s"
           % (len(runs), bad, bad_crc))
    report(four_machines(), "four machines at once")
    slow = []
    for bench in discover_benches():
        if bench.name != "uart_3m":
            source = (c.PROGRAMS / (bench.name + ".pio")).read_text() + "\n.sm 1\nparked: halt\n"
            if not bench.run(c.make_chip, assemble(source)).passed:
                slow.append(bench.name)
    report(set(slow) == NEEDS_60MHZ, "fail at 30 MHz (need a machine alone on its memory): %s" % ", ".join(slow))
    jumps = [bench.name for bench in discover_benches() if not bench.run(SlowJumpChip, c.program(bench.name)).passed]
    report(set(jumps) == NEEDS_ONE_CLOCK_JUMPS, "fail if a taken jump costs two clocks: %s" % ", ".join(jumps))
    problems = assembler_refuses()
    report(not problems, "the assembler refuses %d lines with a field too wide%s"
           % (len(TOO_WIDE), "; " + "; ".join(problems) if problems else ""))
    problems = budget_against_readme()
    report(not problems, "README budget matches budget()%s" % ("; " + "; ".join(problems) if problems else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
