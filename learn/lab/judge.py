# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The judge. It alone compares with the truth, and it shares no code with the host: what a symbol means, the
phases and both bit-level controllers are written again here, from docs of the buses and of the host's result.

  hypothesis  product-machine equivalence of the truth and the learned model, over every symbol
  clone       product of the truth and the clone's whole Verilated state, over every symbol, at pin level
  verdict     exact, refused, missed (outside the stated bound and warned of), or one of three failures:
              wrong copy (the model was exact and the clone is not), broken guarantee, silent wrong copy"""

from collections import deque

from .board import P0, P1, P2, P3, P4

SELECT, DESELECT = 256, 257
START, READ_ACK, READ_NACK, STOP = 256, 512, 513, 514
HIGH, LOW = 0, 1
IDLE, TAKES, SENDS, OVER = 0, 1, 2, 3
PAIRS = 20000
OVERFLOW = 0x80


# ---- what a symbol means

class SpiTruth:
    def __init__(self, table, inputs=None):
        self.t = table
        low = list(range(256)) + [DESELECT]
        self.symbols = ([SELECT], low)
        self.keep(inputs)

    def keep(self, inputs):
        if inputs is not None:
            self.symbols = tuple([s for s in row if s in inputs] for row in self.symbols)

    def step(self, state, s):
        q, phase = state
        if s == SELECT:
            return (self.t.a[q], LOW), 0
        if s == DESELECT:
            return (self.t.b[q], HIGH), 0
        return (self.t.next[q * 256 + s], LOW), self.t.out[q]

    @staticmethod
    def kind(s):
        return 0 if s < 256 else None


class I2cTruth(SpiTruth):
    def __init__(self, table, inputs=None):
        self.t = table
        starts = list(range(START, START + 256))
        self.symbols = (starts, list(range(256)) + starts + [STOP], [READ_ACK, READ_NACK], starts + [STOP])
        self.keep(inputs)

    def step(self, state, s):
        q, phase = state
        if s == STOP:
            return (self.t.c[q], IDLE), 0
        if s == READ_ACK:
            return (self.t.a[q], SENDS), self.t.out[q]
        if s == READ_NACK:
            return (self.t.b[q], OVER), self.t.out[q]
        at = q * 512 + s  # data at 0 to 255, then the bytes after a START, as in the table
        to = self.t.next[at]
        if not self.t.ack[at]:
            return (to, OVER), 1
        return (to, SENDS if s >= START and s & 1 else TAKES), 0

    @staticmethod
    def kind(s):
        if s < START:
            return 0
        if s < READ_ACK:
            return 2 if s & 1 else 1
        return None


TRUTH = {"spi": SpiTruth, "i2c": I2cTruth}


class Minimal:
    """The truth's minimal machine over those symbols, the phase counted as part of a state."""

    def __init__(self, truth):
        start = (0, 0)
        index, order, rows = {start: 0}, [start], []
        while len(rows) < len(order):
            state = order[len(rows)]
            row = []
            for s in truth.symbols[state[1]]:
                to, out = truth.step(state, s)
                if to not in index:
                    index[to] = len(order)
                    order.append(to)
                row.append((s, index[to], out))
            rows.append(row)
        block = [state[1] for state in order]
        count = len(set(block))
        while True:
            ids, now = {}, []
            for q, row in enumerate(rows):
                key = (block[q], tuple((block[to], out) for _, to, out in row))
                now.append(ids.setdefault(key, len(ids)))
            block = now
            if len(ids) == count:
                break
            count = len(ids)
        self.truth = truth
        self.start = block[0]
        self.n = count
        self.phase = [0] * count
        self.rows = [{} for _ in range(count)]
        for q, row in enumerate(rows):
            self.phase[block[q]] = order[q][1]
            if not self.rows[block[q]]:
                self.rows[block[q]] = {s: (block[to], out) for s, to, out in row}

    def uniform(self, learned, defaults):
        """Does every other symbol do exactly what its kind's default does, in every state?"""
        if not defaults:
            return all(s in learned for row in self.rows for s in row)
        for row in self.rows:
            for s, what in row.items():
                if s not in learned:
                    kind = self.truth.kind(s)
                    if kind is None or row.get(defaults[kind]) != what:
                        return False
        return True


# ---- the learned model against the truth

def hypothesis_step(truth, model, rows, learned, q, s):
    found = rows[q].get(s)
    if found is None and s not in learned and model["defaults"]:
        kind = truth.kind(s)
        if kind is not None:
            found = rows[q].get(model["defaults"][kind])
    return found


def hypothesis(truth, minimal, model):
    rows = [{s: (to, out) for s, to, out in row} for row in model["rows"]]
    learned = set(model["alphabet"])
    start = (minimal.start, 0)
    parent = {start: None}
    todo = deque([start])
    while todo:
        pair = todo.popleft()
        p, q = pair
        for s, (to, out) in minimal.rows[p].items():
            found = hypothesis_step(truth, model, rows, learned, q, s)
            if found is None or found[1] != out:
                return {"equal": False, "witness": word_to(parent, pair) + [s], "truth": out,
                        "model": None if found is None else found[1]}
            after = (to, found[0])
            if after not in parent:
                parent[after] = (pair, s)
                todo.append(after)
    return {"equal": True, "pairs": len(parent)}


def word_to(parent, key):
    word = []
    while parent[key] is not None:
        key, s = parent[key]
        word.append(s)
    return word[::-1]


# ---- the controllers

class Fault(Exception):
    pass


class SpiController:
    """Mode 0 with a clock that never pauses between bytes. MISO is taken as SCK rises."""

    SETUP = 3

    def __init__(self, board, khz):
        self.b = board
        period = 60000 // khz
        self.period = period
        self.low, self.high = period // 2, period - period // 2

    def begin(self):
        self.b.far(P0 | P1 | P3, P3)
        self.b.run(200)

    def reset(self):
        self.b.far(P0 | P1 | P3 | P4, P3)
        self.b.run(60)
        self.begin()
        self.b.run(300)

    def op(self, s):
        b, pins = self.b, P0 | P1 | P3
        if s == SELECT:
            b.far(pins, 0)
            b.run(self.period)
            return 0
        if s == DESELECT:
            b.far(pins, P3)
            b.run(self.period)
            seen = b.pins()
            if seen.chip_oe or seen.dev_oe:
                raise Fault("MISO is still driven one period after deselect")
            return 0
        value = 0
        for i in range(8):
            mosi = P1 if s >> (7 - i) & 1 else 0
            b.far(pins, mosi)
            b.run(self.SETUP if i == 0 else self.low)
            seen = b.pins()
            if not (seen.chip_oe | seen.dev_oe) & P2:
                raise Fault("MISO is not driven as SCK rises")
            bit = seen.line & P2
            b.far(pins, mosi | P0)
            b.run(self.high)
            if b.pins().line & P2 != bit:
                raise Fault("MISO moves while SCK is high")
            value = value << 1 | (1 if bit else 0)
        b.far(pins, 0)
        b.run(self.low - self.SETUP)
        return value

    def check(self):
        seen = self.b.pins()
        if seen.chip_oe & ~P2:
            raise Fault("the chip drives a pin that is the controller's")
        if seen.contention:
            raise Fault("two drivers disagree")


class I2cController:
    """Open drain, with UM10204's minimum times where they are the target's problem."""

    def __init__(self, board, khz):
        self.b = board
        self.pulled = 0
        period = 60000 // khz
        if khz > 100:
            self.low, self.setup, self.hold, self.free = 78, 36, 36, 78
            self.high, self.before, self.after = max(36, period - 78 - 18), 24, 4
        else:
            self.low, self.setup, self.hold, self.free = 282, 282, 240, 282
            self.high, self.before, self.after = max(240, period - 282 - 18), 60, 6

    def pull(self, pin, low):
        self.pulled = self.pulled | pin if low else self.pulled & ~pin
        self.b.far(self.pulled, 0)

    def clock_up(self):
        self.pull(P0, False)
        for _ in range(1000):
            self.b.run(1)
            if self.b.pins().line & P0:
                return
        raise Fault("SCL is held low")

    def begin(self):
        self.pulled = 0
        self.b.far(0, 0)
        self.b.run(400)

    def reset(self):
        self.pulled = 0
        self.b.far(P4, 0)
        self.b.run(60)
        self.b.far(0, 0)
        self.b.run(400)

    def bit(self, high, wait):
        b = self.b
        self.pull(P1, not high)
        b.run(wait)
        self.clock_up()
        taken = b.pins().line & P1
        b.run(self.high)
        if b.pins().line & P1 != taken:
            raise Fault("SDA moves while SCL is high")
        self.pull(P0, True)
        b.run(self.after)
        return 1 if taken else 0

    def nine(self, bits, first):
        out = []
        for i, high in enumerate(bits):
            out.append(self.bit(high, first if i == 0 else self.low - self.after))
        self.pull(P1, False)
        self.b.run(self.low - self.after - self.before)
        return out

    def op(self, s):
        b = self.b
        if s == STOP:
            self.pull(P1, True)
            b.run(self.before)
            self.clock_up()
            b.run(self.setup)
            self.pull(P1, False)
            b.run(self.free)
            return 0 if b.pins().line & P1 else 2
        if s == READ_ACK or s == READ_NACK:
            got = self.nine([1] * 8 + [1 if s == READ_NACK else 0], self.before)
            return sum(bit << (7 - i) for i, bit in enumerate(got[:8]))
        first, byte = self.before, s
        if s >= START:
            byte = s - START
            self.pull(P1, False)
            b.run(self.before)
            if not b.pins().line & P1:
                return 2
            self.clock_up()
            b.run(self.setup)
            if not b.pins().line & P1:
                return 2
            self.pull(P1, True)
            b.run(self.hold)
            self.pull(P0, True)
            b.run(self.after)
            first = self.low - self.after
        got = self.nine([byte >> (7 - i) & 1 for i in range(8)] + [1], first)
        if any(got[i] != (byte >> (7 - i) & 1) for i in range(8)):
            raise Fault("something else pulls SDA while the controller sends")
        return got[8]

    def check(self):
        seen = self.b.pins()
        if seen.chip_oe & ~P1:
            raise Fault("the chip drives a pin other than SDA")
        if seen.chip_high:
            raise Fault("the chip drives an open-drain pin high")
        if seen.contention:
            raise Fault("two drivers disagree")


CONTROLLER = {"spi": SpiController, "i2c": I2cController}


def play(board, khz, word):
    """A word put to whatever is on the board by the judge's own controller, from a reset by nRESET."""
    control = CONTROLLER[board.bus](board, khz)
    control.reset()
    board.clear()
    out = []
    for s in word:
        out.append(control.op(s))
        control.check()
    return out


# ---- the clone against the truth

def clone(board, image, minimal, khz, pairs=PAIRS):
    """Every reachable pair of a true state and a state of the whole chip, and every symbol from each."""
    board.unplug()
    if not board.load(image):
        return {"equal": False, "witness": [], "fault": "the image does not load", "complete": False}
    control = CONTROLLER[board.bus](board, khz)
    control.begin()
    board.clear()
    start = (minimal.start, board.save())
    parent = {start: None}
    todo = deque([start])
    steps, resets = 0, True
    complete = True
    while todo:
        key = todo.popleft()
        state, chip = key
        wanted = list(minimal.rows[state].items())
        if resets:
            wanted.append((None, (minimal.start, None)))
        for s, (to, out) in wanted:
            board.restore(chip)
            if board.bus == "i2c":  # between symbols the controller holds SCL low, except when idle
                control.pulled = 0 if minimal.phase[state] == IDLE else P0
            fault = None
            try:
                if s is None:
                    control.reset()
                    got = None
                else:
                    got = control.op(s)
                control.check()
                if board.pins().status & OVERFLOW:
                    raise Fault("OVERFLOW")
                if got != out:
                    fault = "answers %r, not %r" % (got, out)
            except Fault as problem:
                fault = str(problem)
            steps += 1
            if fault:
                board.far(0, 0)
                board.clear()
                return {"equal": False, "witness": word_to(parent, key) + [s], "fault": fault, "steps": steps,
                        "pairs": len(parent), "complete": False}
            after = (to, board.save())
            if after not in parent:
                if len(parent) >= pairs:
                    complete = False
                    continue
                parent[after] = (key, s)
                todo.append(after)
    board.far(0, 0)
    return {"equal": True, "complete": complete, "steps": steps, "pairs": len(parent)}


# ---- the verdict

EXACT, REFUSED, MISSED = "exact", "refused", "missed, warned"
WRONG_CLONE, BROKEN, SILENT, ERROR = "WRONG COPY", "BROKEN GUARANTEE", "SILENT WRONG COPY", "ERROR"
FAILURES = (WRONG_CLONE, BROKEN, SILENT, ERROR)


def impure(table):
    """Is the device outside every assumption: noise, time, or a reset that does not clear it?"""
    return bool(table.quirks or table.mask)


def judge(board, table, result, rates=None, pairs=PAIRS):
    told = result.get("told") or {}
    truth = TRUTH[table.bus](table, told.get("inputs"))
    minimal = Minimal(truth)
    report = {"true_states": minimal.n, "clone": [], "model": None}
    model = result.get("model")
    if model:
        report["model"] = hypothesis(truth, minimal, model)
        report["learned_states"] = len(model["rows"])
        n, m = len(model["rows"]), result["stats"].get("extra", 0)
        uniform = minimal.uniform(set(model["alphabet"]), model["defaults"])
        report["uniform"] = uniform
        report["inside"] = not impure(table) and (
            (model["inputs"] is None and minimal.n <= n) or (minimal.n <= n + m and uniform))
    if not result["ok"]:
        if result["image"] is not None:
            report["verdict"] = SILENT
        elif model and not report["model"]["equal"] and report["inside"]:
            report["verdict"] = BROKEN
        else:
            report["verdict"] = ERROR if result["code"] == "error" else REFUSED
        return report
    same = report["model"]["equal"]
    if same and impure(table):  # the table is only part of what is on the board, so no copy of it is exact
        report["verdict"] = MISSED if result["warnings"] else SILENT
        return report
    if same:
        for khz in rates or (told["serve"] or told["rate"],):
            found = clone(board, result["image"], minimal, khz, pairs)
            found["khz"] = khz
            report["clone"].append(found)
            same = same and found["equal"] and found["complete"]
        report["verdict"] = EXACT if same else WRONG_CLONE
        return report
    if report["inside"]:
        report["verdict"] = BROKEN
    elif not result["warnings"] or not result["guarantee"]:
        report["verdict"] = SILENT
    else:
        report["verdict"] = MISSED
    return report
