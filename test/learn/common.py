# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""What the learn-and-become tests share."""

import sys
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from learn.host import session  # noqa: E402
from learn.lab import devices, judge  # noqa: E402

OPERANDS = {"spi": {0: 2, 1: 0, 2: 0, 3: 1, 4: 1}, "i2c": {0: 2, 1: 1, 2: 1, 3: 0, 4: 0, 5: 0, 6: 1}}
PAUSE = {"spi": 4, "i2c": 6}


def operations(bus, data):
    """The query engine's operations in `data`."""
    at, sizes = 0, OPERANDS[bus]
    while at < len(data):
        size = sizes[data[at]]
        yield data[at], bytes(data[at + 1:at + 1 + size])
        at += 1 + size


def symbol(bus, op, operand):
    if bus == "spi":
        return operand[0] if op == 3 else 255 + op
    if op == 1:
        return 256 + operand[0]
    if op == 2:
        return operand[0]
    return 509 + op  # 3 read+ACK, 4 read+NACK, 5 STOP


class Ideal:
    """A link that answers as the query engine would, straight from a table. The learner's logic can then be
    tested on hundreds of devices. The RTL tests show that the real engine gives the same answers."""

    def __init__(self, table, flip=None, stuck=False):
        self.bus = table.bus
        self.truth = judge.TRUTH[table.bus](table)
        self.allowed = [set(row) for row in self.truth.symbols]
        self.flip, self.stuck = flip, stuck
        self.images = []
        self.answered = 0

    def load(self, image):
        self.images.append(image)
        return True

    def exchange(self, data, wanted):
        out, state, legal = bytearray(), (0, 0), True
        for op, operand in operations(self.bus, data):
            if op == 0:
                state, legal = (0, 0), True
                continue
            if op == PAUSE[self.bus]:
                continue
            s = symbol(self.bus, op, operand)
            legal = legal and s in self.allowed[state[1]]
            answer = 0
            if legal:
                state, answer = self.truth.step(state, s)
            self.answered += 1
            if self.flip and self.answered % self.flip == 0:
                answer ^= 1
            out.append(answer)
        return (bytes(out) if len(out) == wanted else None), (b"\x01" if self.stuck else b"")


def learn(table, **told):
    return session.run(dict(told, bus=table.bus), Ideal(table))


def verdict(table, result):
    """The judge's word on the model alone: exact, refused, outside the bound, or one of its failures."""
    if not result["ok"]:
        return judge.ERROR if result["code"] == "error" else judge.REFUSED
    truth = judge.TRUTH[table.bus](table, result["told"]["inputs"])
    minimal = judge.Minimal(truth)
    model = result["model"]
    if judge.hypothesis(truth, minimal, model)["equal"]:
        return judge.EXACT
    n, m = len(model["rows"]), result["stats"]["extra"]
    uniform = minimal.uniform(set(model["alphabet"]), model["defaults"])
    if (model["inputs"] is None and minimal.n <= n) or (minimal.n <= n + m and uniform):
        return judge.BROKEN
    return judge.MISSED if result["warnings"] else judge.SILENT


def renumbered(table):
    """The same behaviour from different innards: every state twice, and in another order."""
    n = table.n
    order = [0] + list(range(2 * n - 1, 0, -1))  # which original (or original's twin) each new number is
    number = {old: new for new, old in enumerate(order)}
    width = len(table.next) // n

    def to(old):
        return number[old + n * ((old * 7 + 3) % 2)] if old else 0

    def column(values):
        return None if values is None else array("i", [to(values[old % n]) for old in order])

    out = bytes(table.out[old % n] for old in order)
    next_ = array("i", [to(table.next[(old % n) * width + x]) for old in order for x in range(width)])
    ack = None if table.ack is None else bytes(
        table.ack[(old % n) * width + x] for old in order for x in range(width))
    return devices.Table(table.name, table.bus, order, out, next_, column(table.a), column(table.b),
                         column(table.c), ack)
