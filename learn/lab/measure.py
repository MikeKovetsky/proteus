# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""What fits, and what it costs the host: table entries for each state, and the host's memory.

Capacity is the generator's alone, on machines it is given (learn/lab/cloning.py), with every program that fits
judged on the RTL. Memory is CPython's, by tracemalloc: what is alive, sampled as the questions go out after a
full collection, which also empties CPython's free lists. It says nothing exact of MicroPython, which hasn't run
this."""

import gc
import tracemalloc

from ..host import clone_i2c, clone_spi, session  # noqa: F401  (loaded before anything is counted)
from ..host.bus import Refused
from . import board, cloning, judge, passive

PROBE = (4, 8, 16, 32, 64, 100)  # 64 and 100 had 54 and 90 minimal states there, and didn't fit
TWO_BYTE = (8, 16, 32, 64, 100, 128, 160)
TWO_BYTE_INPUTS = [0x5A, 0xA5, judge.SELECT]
SCRAMBLED = {"spi": (10, 20, 40, 60, 80, 100), "i2c": (4, 8, 12, 16, 20, 24)}
SERVE = {"spi": 2000, "i2c": 400}
EVERY = 250  # questions between two samples of what is alive


class Sampled(passive.Direct):
    """The link, noting as questions pass how much is alive."""

    def __init__(self, on):
        passive.Direct.__init__(self, on)
        self.asked = self.alive = 0

    def exchange(self, data, wanted):
        if self.asked % EVERY == 0:
            gc.collect()
            self.alive = max(self.alive, tracemalloc.get_traced_memory()[0])
        self.asked += 1
        return passive.Direct.exchange(self, data, wanted)


def sized(table, inputs=None):
    """One line of the capacity table."""
    out = {"name": table.name, "inputs": inputs is not None,
           "states": judge.Minimal(judge.TRUTH[table.bus](table, inputs)).n}
    try:
        made, result = cloning.generate(table, SERVE[table.bus], inputs)
    except Refused as refusal:
        return dict(out, entries=None, verdict="refused: " + refusal.code)
    out.update(entries=made["entries"], verdict="does not fit")
    if result["ok"]:
        with board.Board(table.bus) as on:
            out["verdict"] = judge.judge(on, table, result, [SERVE[table.bus]])["verdict"]
    return out


def capacity():
    for raw in PROBE:
        yield sized(cloning.probes_machine(raw), TWO_BYTE_INPUTS)
    for raw in TWO_BYTE:
        for inputs in (TWO_BYTE_INPUTS, None):
            yield sized(cloning.two_byte(raw, raw), inputs)
    for bus in ("spi", "i2c"):
        for states in SCRAMBLED[bus]:
            yield sized(cloning.scrambled(bus, states, 4, states))


def memory(table, told):
    """The most the host held at once while it learned the table through the pins, in bytes."""
    with board.Board(table.bus) as on:
        on.plug(table)
        link = Sampled(on)
        tracemalloc.start()
        try:
            result = session.run(dict(told, bus=table.bus), link)
            most = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    return {"ok": result["ok"], "peak": most, "alive": link.alive, "queries": result["stats"].get("queries")}
