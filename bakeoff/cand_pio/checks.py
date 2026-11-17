"""Checks of cand_pio beyond the benches, with its own signal generators.

    python3 -m bakeoff.cand_pio.checks

1. eth10_rx over every clock phase and +-200 ppm.   2. usb_ls_rx over phases and +-1.5%.
3. Four machines at once.   4. Which programs still pass at 30 MHz (a second machine enabled on memory A).
"""
from __future__ import annotations

import random
import zlib

from ..harness.sim import MASK, Sim, payload
from ..run import discover_benches
from . import candidate as c
from .asm import assemble


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


def main() -> int:
    rnd = random.Random(7)
    runs = [(ppm, i / 8, size) for ppm in (-200, -100, 0, 100, 200) for i in range(24) for size in (60, 61, 300)]
    bad = sum(not run_eth(bytes(rnd.randrange(256) for _ in range(size)), ppm, ph) for ppm, ph, size in runs)
    print("eth10_rx: %d runs over phase and ppm, %d failed; flipped bit reported: %s"
          % (len(runs), bad, run_eth(bytes(range(64)), 200, 0.3, flip=True)))
    datas = [b"", bytes([0xFF] * 8), bytes(rnd.randrange(256) for _ in range(8)), bytes(5), bytes([0x7E, 0xFC, 0x3F])]
    runs = [(r, i * 2.5, d, p) for r in (0.985, 0.99, 1.0, 1.01, 1.015) for i in range(16) for d in datas
            for p in (0xC3, 0x4B)]
    bad = sum(not run_usb(p, d, r, ph) for r, ph, d, p in runs)
    print("usb_ls_rx: %d runs over phase and rate, %d failed; bad CRC reported: %s"
          % (len(runs), bad, run_usb(0xC3, b"hello", 1.0, 3.0, flip=True)))
    print("four machines at once: %s" % ("pass" if four_machines() else "FAIL"))
    slow = []
    for bench in discover_benches():
        if bench.name != "uart_3m":
            source = (c.PROGRAMS / (bench.name + ".pio")).read_text() + "\n.sm 1\nparked: halt\n"
            if not bench.run(c.make_chip, assemble(source)).passed:
                slow.append(bench.name)
    print("fail at 30 MHz (need a machine alone on its memory): %s" % ", ".join(slow))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
