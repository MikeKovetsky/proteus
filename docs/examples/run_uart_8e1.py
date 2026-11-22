"""Run the 8E1 UART example on the golden model and judge it with a decoder written here.

    python3 docs/examples/run_uart_8e1.py

It compiles uart_8e1_tx.fsm and uart_8e1_rx.fsm with the project's compiler and runs the images on the
cycle-accurate model of the chip (bakeoff/cand_table/chip.py), inside the bake-off's model of the pads and the
host (bakeoff/harness/sim.py). Then it runs both machines in one image, as the chip runs two protocols at once.
It prints what went in and what came out, and exits with status 1 if anything is wrong.
Standard library only; Python 3.9 or later.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from bakeoff.cand_table.chip import TableChip  # noqa: E402
from bakeoff.cand_table.compiler import compile_file, compile_text  # noqa: E402
from bakeoff.harness.sim import Sim  # noqa: E402

HERE = Path(__file__).resolve().parent
BIT = 60_000_000 / 115200  # 520.83 clocks: the true bit time, not the program's rounded 521
TX_PIN, RX_PIN = 0, 1
TX_DATA = bytes([0x00, 0x55, 0xA7, 0xFF, 0x31, 0x80])  # what the host gives the transmitter
RX_FRAMES = [(0x12, ""), (0xC3, ""), (0x5A, "parity"), (0x7E, ""), (0x2D, "stop"), (0x80, "")]  # and what is wrong


def parity(byte: int) -> int:
    """The even-parity bit: 1 when the byte has an odd number of 1s."""
    return bin(byte).count("1") & 1


def frame(byte: int, wrong: str = "") -> List[int]:
    """One 8E1 frame as line levels, one per bit: start, 8 data bits LSB first, parity, stop. `wrong` names the
    bit to spoil. A stop bit of 0 gets one bit of high line after it, so that a start bit can follow."""
    end = [0, 1] if wrong == "stop" else [1]
    return [0] + [(byte >> n) & 1 for n in range(8)] + [parity(byte) ^ (wrong == "parity")] + end


def decode(wave: List[int], pin: int) -> Tuple[bytes, List[str]]:
    """Read 8E1 frames off `pin`, sampling the middle of each bit from the frame's own start edge."""
    mask, out, problems, i = 1 << pin, bytearray(), [], 1
    while i < len(wave):
        if not (wave[i - 1] & mask and not wave[i] & mask):
            i += 1
            continue
        if i + int(11 * BIT) > len(wave):  # a frame the run cuts off is still a frame the chip sent
            problems.append("frame at cycle %d: the run ends before the frame does" % i)
            break
        bits = [1 if wave[i + int((k + 0.5) * BIT)] & mask else 0 for k in range(11)]
        byte = sum(bits[1 + n] << n for n in range(8))
        if bits[0] != 0 or bits[10] != 1:
            problems.append("frame at cycle %d: bad start or stop bit" % i)
        if bits[9] != parity(byte):
            problems.append("frame at cycle %d: parity bit %d for byte %02x" % (i, bits[9], byte))
        for j in range(i + 1, i + int(10.5 * BIT)):  # every edge must sit on a bit boundary, within 2%
            if (wave[j] ^ wave[j - 1]) & mask:
                k = round((j - i) / BIT)
                if abs(j - i - k * BIT) > max(2.0, 0.02 * k * BIT):
                    problems.append("frame at cycle %d: edge at %d is off the bit grid" % (i, j))
        slack = int(0.02 * 11 * BIT)  # and the line must stay high until 11 bits after the start edge, within 2%
        if any(not wave[j] & mask for j in range(i + int(10 * BIT) + slack, i + int(11 * BIT) - slack)):
            problems.append("frame at cycle %d: the stop bit is cut short" % i)
        out.append(byte)
        i += int(10.5 * BIT)
    return bytes(out), problems


def run(name: str, image: bytes, tx: bool, rx: bool, speed: float = 1.0) -> List[str]:
    """Give the transmitter TX_DATA on channel 0 and/or drive RX_FRAMES on P1 at `speed` times 115200 baud."""
    sim = Sim(TableChip(), image)  # all pins pulled high, the program loaded
    if tx:
        sim.send(0, TX_DATA)  # the host writes to channel 0
    line = [level for byte, wrong in RX_FRAMES for level in frame(byte, wrong)] if rx else []
    start, bit = 2000, BIT / speed
    for cycle in range(int(max(len(TX_DATA) * 12 * BIT, start + (len(line) + 2) * bit)) + 2000):
        index = int((cycle - start) / bit)
        sim.set(RX_PIN, line[index] if cycle >= start and index < len(line) else 1)
        sim.tick()
    problems, label = [], "%s at %+.0f%%" % (name, (speed - 1) * 100) if rx else name
    if tx:
        got, problems = decode(sim.wave, TX_PIN)
        print("%s: sent %s, decoded %s from P0" % (label, TX_DATA.hex(" "), got.hex(" ")))
        if got != TX_DATA:
            problems.insert(0, "decoded the wrong bytes")
    if rx:
        good = bytes(b for b, wrong in RX_FRAMES if not wrong)
        bad = bytes(b for b, wrong in RX_FRAMES if wrong)
        ch1, ch2 = sim.received(1), sim.received(2)
        print("%s: channel 1 got %s (wanted %s), channel 2 got %s (wanted %s)"
              % (label, ch1.hex(" "), good.hex(" "), ch2.hex(" "), bad.hex(" ")))
        if (ch1, ch2) != (good, bad):
            problems.append("wrong bytes on channels 1 and 2")
    if sim.contention:
        problems.append("%d pin-cycles where the chip and the bench drove opposite levels" % sim.contention)
    return ["%s: %s" % (label, p) for p in problems]


def main() -> int:
    tx = compile_file(HERE / "uart_8e1_tx.fsm")
    rx = compile_file(HERE / "uart_8e1_rx.fsm")
    # Both machines in one image. They take turns on the table, so each one steps every second clock and
    # its numbers count turns of 2 clocks: BIT is halved, and nothing else changes.
    both = (HERE / "uart_8e1_tx.fsm").read_text() + (HERE / "uart_8e1_rx.fsm").read_text()
    old = "const BIT = 60000000 / 115200"
    if both.count(old) != 2:
        print("FAIL  both programs must have the line `%s`, for the two-machine image" % old)
        return 1
    both = both.replace(old, old + " / 2")
    duplex = compile_text(both, "uart_8e1_tx.fsm + uart_8e1_rx.fsm")
    for name, result in (("tx", tx), ("rx", rx), ("duplex", duplex)):
        print("%s: %d states in %d entries, a %d-byte image" % (name, result.states, result.entries,
                                                                 len(result.image)))
    problems = run("tx", tx.image, tx=True, rx=False)
    for speed in (1.02, 0.98):  # the far end's clock 2% fast, then 2% slow
        problems += run("rx", rx.image, tx=False, rx=True, speed=speed)
        problems += run("duplex", duplex.image, tx=True, rx=True, speed=speed)
    for line in problems:
        print("FAIL  " + line)
    print("uart_8e1: %s" % ("ok" if not problems else "%d problems" % len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
