# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Table generation on its own: the clone of a machine that is given, not learned.

It measures the encoding and tests the generators without the learner in the way. It is the one place in the
lab that hands a truth to the host's code, and that code is then the generator alone. learn/lab/jail.py, where
learning happens, does not import it."""

import random
from array import array

from ..host import session
from ..host.model import Model
from . import devices, judge


def described(table, bus, inputs=None):
    """The table's minimal machine as a model: the commonest column of each kind is its default."""
    truth = judge.TRUTH[table.bus](table, inputs)
    minimal = judge.Minimal(truth)
    order = [minimal.start] + [q for q in range(minimal.n) if q != minimal.start]
    number = {q: i for i, q in enumerate(order)}
    columns, controls = {}, []
    for s in sorted({s for row in minimal.rows for s in row}):
        kind = truth.kind(s)
        if kind is None or inputs is not None:
            controls.append(s)
        else:
            column = tuple(row.get(s) for row in minimal.rows)
            columns.setdefault(kind, {}).setdefault(column, []).append(s)
    alphabet, defaults = list(controls), []
    for kind in range(bus.KINDS if inputs is None else 0):
        if kind not in columns:  # no state takes that kind, so any of it will do
            defaults.append(bus.defaults()[kind])
            alphabet.append(defaults[-1])
            continue
        usual = max(columns[kind].values(), key=len)
        defaults.append(usual[0])
        alphabet += [group[i] for group in columns[kind].values() for i in range(len(group))
                     if group is not usual or i == 0]
    rows = [{s: (number[to], out) for s, (to, out) in minimal.rows[q].items() if s in alphabet} for q in order]
    return alphabet, defaults, [minimal.phase[q] for q in order], rows


def generate(table, serve, inputs=None):
    """(what the generator returns, the result as the judge takes it)."""
    told = session.settings({"bus": table.bus, "serve": serve, "inputs": inputs})
    bus = session.BUSES[table.bus](told)
    alphabet, defaults, phases, rows = described(table, bus, inputs)
    model = Model(bus, phases, rows, alphabet, defaults, [()] * len(rows), [()] * len(rows))
    made = bus.clone(model)
    result = {"ok": made["image"] is not None, "code": "ok", "image": made["image"], "told": told,
              "model": session.exported(model, bus), "stats": {"extra": 0, "entries": made["entries"]},
              "guarantee": ["given"], "warnings": ["given"]}
    return made, result


def scrambled(bus, states, special, seed, spread=3, answers=4):
    """A table with no meaning: `special` bytes act, each in its own way, and the others go a common way."""
    rng = random.Random(seed)
    width = 256 if bus == "spi" else 512
    marked = rng.sample(range(256), special)
    values = [rng.randrange(256) for _ in range(answers)]
    reach = [rng.sample(range(states), min(states, spread)) for _ in range(states)]
    out = bytes(rng.choice(values) for _ in range(states))
    next_, ack = array("i"), bytearray()
    for q in range(states):
        for high in range(0, width, 256):
            to = [rng.randrange(states) for _ in range(2)]
            takes = [rng.random() < (0.5 if high == 0 else 0.2) for _ in range(2)]
            for x in range(256):
                side = x & 1 if high else 0  # addresses have a default for writes and one for reads
                if x in marked or (high and x & 0xFE in marked):
                    next_.append(reach[q][(x * 7 + q) % len(reach[q])])
                    ack.append(1 if (x * 13 + q * 3) % 4 else 0)
                else:
                    next_.append(to[side])
                    ack.append(1 if takes[side] else 0)

    def column():
        return array("i", [rng.randrange(states) for _ in range(states)])

    name = "scrambled %s %d/%d #%d" % (bus, states, special, seed)
    if bus == "spi":
        return devices.Table(name, bus, list(range(states)), out, next_, column(), column())
    return devices.Table(name, bus, list(range(states)), out, next_, column(), column(), column(), bytes(ack))


def two_byte(states, seed):
    """Two byte values (A5 and any other) and two answers: the sizes of the first feasibility probe, on SPI."""
    rng = random.Random(seed)
    to = [(rng.randrange(states), rng.randrange(states)) for _ in range(states)]
    next_ = array("i", [to[q][1 if x == 0xA5 else 0] for q in range(states) for x in range(256)])
    out = bytes(rng.choice((0x21, 0x4F)) for _ in range(states))
    same = array("i", range(states))
    return devices.Table("two-byte %d" % states, "spi", list(range(states)), out, next_, same, same)


def probes_machine(states):
    """One of the random machines of the first feasibility probe (the same seed and graph), carried to SPI. A
    bit becomes one of two bytes, and a step's answer bit the byte sent next, since an SPI target decides a byte
    before it has the one coming in. So a state of the probe's can become two here."""
    rng = random.Random(states)
    steps = [tuple((rng.randrange(states), rng.getrandbits(1)) for _ in range(2)) for _ in range(states)]
    next_ = array("i")
    for q in range(2 * states):
        to = [2 * after + answer for after, answer in steps[q // 2]]
        for x in range(256):
            next_.append(to[1 if x == 0xA5 else 0])
    out = bytes(0x4F if q & 1 else 0x21 for q in range(2 * states))
    same = array("i", range(2 * states))
    return devices.Table("the probe's %d" % states, "spi", list(range(2 * states)), out, next_, same, same)
