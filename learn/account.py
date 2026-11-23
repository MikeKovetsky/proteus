# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""A readable account of a learned device, for a person debugging it or working out what it is.

It reads the model the host exports (`model` in a result of learn/host/session.py: the phases, the symbols
learned in full, their kinds' defaults, the told inputs and the rows) and nothing else, and writes it three ways:

  map      the device as a person would write it down: its modes, which are the states it rests in between
           transactions, and for each mode what a transaction does byte by byte, the bytes that act alike
           grouped. Each mode says the shortest way to it from reset. It suits a register device.
  states   every state, where each symbol takes it and what it answers, as text. It suits a protocol
           machine, such as a lock, whose bytes matter deeper than the map follows.
  dot      the states as a Graphviz graph, for `dot -Tsvg` where Graphviz is installed

`readable` picks the map, or the state list where the map would have to stop short.

It says only what the model says. Where the model is wrong, so is this account, and the host's guarantee and
warnings say how far to trust it. The bus grammar is written again here, as learn/lab/judge.py writes it, so this
is a second reading of the exported model and imports nothing of the host's. It lives outside learn/host, so
changing it changes nothing that was frozen."""

from collections import OrderedDict, deque

SELECT, DESELECT = 256, 257
START, READ_ACK, READ_NACK, STOP = 256, 512, 513, 514
IDLE, TAKES, SENDS, ENDED = 0, 1, 2, 3  # I2C's phases. SPI's are 0 CS high and 1 CS low
STREAM = 8  # bytes a stream is followed before it is cut short
DEEPEST = 3  # bytes that matter in a row before the rest is left to the state list


def hexed(values):
    return " ".join("%02X" % v for v in values)


def ranges(values):
    """Byte values as runs: 00-0F 12 40-7F."""
    values = sorted(values)
    out, i = [], 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and values[j + 1] == values[j] + 1:
            j += 1
        out.append("%02X" % values[i] if i == j else "%02X-%02X" % (values[i], values[j]))
        i = j + 1
    return " ".join(out)


def stream(answers, cycle, verb):
    """`sends 4D 4B, then 00 repeated`, from the answers and where they start to repeat."""
    if cycle is None:
        return "%s %s" % (verb, hexed(answers)) + (" ..." if answers else "")
    fixed, looped = answers[:cycle], answers[cycle:]
    if len(looped) == 1:
        while fixed and fixed[-1] == looped[0]:
            fixed = fixed[:-1]
    if not looped:
        return "%s %s" % (verb, hexed(fixed))
    return "%s %s%s%s" % (verb, hexed(fixed) + ", then " if fixed else "", hexed(looped),
                          " repeated" if len(looped) == 1 else " over and over")


class Machine:
    """The exported model, with the defaults applied, over the symbols the bus allows."""

    def __init__(self, bus, model):
        if bus not in ("spi", "i2c"):
            raise ValueError("the bus is spi or i2c, not %r" % (bus,))
        self.bus = bus
        self.rows = [{s: (to, out) for s, to, out in row} for row in model["rows"]]
        self.phases = list(model["phases"])
        self.learned = set(model["alphabet"])
        self.defaults = list(model["defaults"])
        self.inputs = None if model["inputs"] is None else set(model["inputs"])
        self.n = len(self.rows)

    def kind(self, s):
        if s < 256:
            return 0
        if self.bus == "i2c" and s < READ_ACK:
            return 2 if s & 1 else 1
        return None

    def allows(self, phase, s):
        if self.inputs is not None and s not in self.inputs:
            return False
        if self.bus == "spi":
            return s == SELECT if phase == 0 else s != SELECT
        if phase == SENDS:
            return s in (READ_ACK, READ_NACK)
        if START <= s < READ_ACK:
            return True
        if phase == TAKES:
            return s < START or s == STOP
        return phase == ENDED and s == STOP

    def symbols(self, q, low=0, high=None):
        high = (258 if self.bus == "spi" else 515) if high is None else high
        return [s for s in range(low, high) if self.allows(self.phases[q], s)]

    def step(self, q, s):
        """(next state, answer), or None where the bus allows no such symbol here."""
        if not self.allows(self.phases[q], s):
            return None
        found = self.rows[q].get(s)
        if found is None and s not in self.learned and self.defaults:
            kind = self.kind(s)
            if kind is not None:
                found = self.rows[q].get(self.defaults[kind])
        return found

    def grouped(self, q, symbols):
        """[((next state, answer), symbols, rest)]: the symbols that act alike together, the largest group
        last and called the rest when there is more than one."""
        out = OrderedDict()
        for s in symbols:
            found = self.step(q, s)
            if found is not None:
                out.setdefault(found, []).append(s)
        items = list(out.items())
        if not items:
            return []
        largest = max(range(len(items)), key=lambda i: (len(items[i][1]), -i))
        rest = items.pop(largest)
        return [(key, symbols, False) for key, symbols in items] + [(rest[0], rest[1], bool(items))]

    def show(self, s):
        if self.bus == "spi":
            return {SELECT: "select", DESELECT: "deselect"}.get(s, "%02X" % s)
        if s < START:
            return "%02X" % s
        if s < READ_ACK:
            return "START %02X+%s" % ((s - START) >> 1, "R" if s & 1 else "W")
        return ("read+ACK", "read+NACK", "STOP")[s - READ_ACK]

    def shown(self, symbols):
        """Symbols, with runs of bytes and of addresses compressed."""
        data = [s for s in symbols if s < 256]
        writes = [(s - START) >> 1 for s in symbols if self.bus == "i2c" and START <= s < READ_ACK and not s & 1]
        reads = [(s - START) >> 1 for s in symbols if self.bus == "i2c" and START <= s < READ_ACK and s & 1]
        rest = [self.show(s) for s in symbols if s >= 256 and (self.bus == "spi" or s >= READ_ACK)]
        parts = [ranges(data)] if data else []
        if writes:
            parts.append("START %s+W" % ranges(writes))
        if reads:
            parts.append("START %s+R" % ranges(reads))
        return ", ".join(parts + rest)


# ---- the map

class Mapper:
    def __init__(self, machine):
        self.m = machine
        self.modes, self.reach = [], {}
        seen, todo = {0: ()}, deque([0])
        while todo:  # breadth first from reset, so each mode gets the shortest way there
            q = todo.popleft()
            if machine.phases[q] == 0:
                self.modes.append(q)
                self.reach[q] = seen[q]
            for (to, _), symbols, _ in machine.grouped(q, machine.symbols(q)):
                if to not in seen:
                    seen[to] = seen[q] + (symbols[0],)
                    todo.append(to)
        self.number = {q: i for i, q in enumerate(self.modes)}

    def mode(self, q):
        return "mode %d" % self.number[q] if q in self.number else "state s%d" % q

    def how(self, word):
        """A word as transactions: `06 | 5E` on SPI, `START 4B+W 01 01 STOP` on I2C."""
        m = self.m
        if not word:
            return "reset"
        if m.bus == "i2c":
            return "reset, then " + " ".join(m.show(s) for s in word)
        out, now = [], []
        for s in word:
            if s == SELECT:
                now = []
            elif s == DESELECT:
                out.append(" ".join(now) or "(no byte)")
            else:
                now.append("%02X" % s)
        return "reset, then the transaction%s %s" % ("s" if len(out) > 1 else "", " | ".join(out))

    def lines(self):
        m = self.m
        out = ["%s, %d states learned, %d of them modes: states it rests in between transactions." % (
            "SPI mode 0" if m.bus == "spi" else "I2C", m.n, len(self.modes))]
        if m.inputs is not None:
            out.append("Only the inputs the host was told of: %s. Nothing is known of any other." %
                       m.shown(sorted(m.inputs)))
        for q in self.modes:
            out.append("")
            out.append("%s, reached by %s." % (self.mode(q).capitalize(), self.how(self.reach[q])))
            out += self.spi_mode(q) if m.bus == "spi" else self.i2c_starts(q, 2, 1, "")
        return out

    # SPI: a select, bytes, a deselect

    def sends(self, q):
        for s in range(256):
            found = self.m.step(q, s)
            if found is not None:
                return found[1]
        return None

    def spi_mode(self, q):
        found = self.m.step(q, SELECT)
        if found is None:
            return ["  It is never selected here."]
        sends = self.sends(found[0])
        return ["  CS falls. MISO sends %s during the first byte, which does this:" % (
            "nothing" if sends is None else "%02X" % sends)] + self.spi_bytes(found[0], 4, 1)

    def spi_bytes(self, q, indent, depth):
        m = self.m
        out = []
        groups = m.grouped(q, m.symbols(q, 0, 256))
        for (to, _), symbols, rest in groups:
            label = "any other" if rest else ranges(symbols) if len(groups) > 1 or len(symbols) < 256 else "any byte"
            out += self.spi_after(to, " " * indent + "%-12s " % label, indent, depth)
        return out

    def spi_after(self, t, head, indent, depth):
        """What follows a byte that took the device to state t: what MISO sends during the bytes after it, and
        where CS rising leaves it, as far as the bytes after it all act alike."""
        m = self.m
        answers, ends, index, at = [], [], {}, t
        branch = cycle = None
        while True:
            if at in index:
                cycle = index[at]
                break
            index[at] = len(answers)
            down = m.step(at, DESELECT)
            ends.append(down[0] if down else None)
            groups = m.grouped(at, m.symbols(at, 0, 256))
            if len(groups) != 1:
                branch = at if groups else None
                break
            if len(answers) >= STREAM:
                break
            answers.append(self.sends(at))
            at = groups[0][0][0]
        parts = []
        if answers:
            parts.append(stream(answers, cycle, "sends"))
        parts.append(self.spi_ends(ends, cycle))
        text = head + "; ".join(parts)
        if branch is None:
            return [text]
        if depth >= DEEPEST:
            return [text + "; the next byte matters: see s%d in the state list" % branch]
        return [text + "; the next byte matters, and MISO sends %02X during it:" % self.sends(branch)] + \
            self.spi_bytes(branch, indent + 4, depth + 1)

    def spi_ends(self, ends, cycle):
        """Where CS rising leaves the device, by how many bytes came after the one described."""
        if all(e is None for e in ends):
            return "CS must not rise"
        if len(set(ends)) == 1:
            return "CS rises: %s" % self.mode(ends[0])
        parts, i = [], 0
        while i < len(ends):
            j = i
            while j + 1 < len(ends) and ends[j + 1] == ends[i]:
                j += 1
            if i == 0:
                when = "right after it" if j == 0 else "after up to %d more bytes" % j
            elif j == len(ends) - 1:
                when = "after %d or more bytes" % i
            else:
                when = "after %d more byte%s" % (i, "" if i == 1 else "s") if i == j else \
                    "after %d to %d more bytes" % (i, j)
            parts.append("%s, %s" % (when, "it must not" if ends[i] is None else self.mode(ends[i])))
            i = j + 1
        return "CS rises " + "; ".join(parts)

    # I2C: START and an address, bytes or reads, STOP

    def i2c_starts(self, q, indent, depth, repeated):
        """What a START does from q. After a byte (`repeated`), only the reads are shown: a repeated START that
        writes acts as a STOP and a START would, and the state list has it."""
        m = self.m
        out = []
        for (to, answer), symbols, rest in m.grouped(q, m.symbols(q, START, READ_ACK)):
            label = repeated + ("START at any other address" if rest else m.shown(symbols))
            if repeated and (answer or m.phases[to] != SENDS):
                continue
            if answer:
                out.append(" " * indent + label + ": NACK")
            elif m.phases[to] == SENDS:
                out.append(" " * indent + label + ": ACK, then " + self.i2c_reads(to))
            else:
                more = self.i2c_writes(to, indent + 4, depth)
                out.append(" " * indent + label + ": ACK" + ("; then a byte:" if more else ""))
                out += more
        return out

    def i2c_reads(self, r):
        m = self.m
        answers, index, at, cycle, ends = [], {}, r, None, set()
        while len(answers) < STREAM:
            if at in index:
                cycle = index[at]
                break
            index[at] = len(answers)
            last = m.step(at, READ_NACK)
            stop = last and m.step(last[0], STOP)
            if stop:
                ends.add(stop[0])
            found = m.step(at, READ_ACK)
            if found is None:
                break
            answers.append(found[1])
            at = found[0]
        text = stream(answers, cycle, "reads")
        if ends:
            text += "; NACK and STOP: " + " or ".join(self.mode(e) for e in sorted(ends))
        return text

    def i2c_writes(self, w, indent, depth):
        """The bytes after a write address, and what each leads to."""
        m = self.m
        out = []
        groups = m.grouped(w, m.symbols(w, 0, 256))
        for (to, answer), symbols, rest in groups:
            label = " " * indent + "%-12s " % ("any other" if rest else ranges(symbols))
            if answer:
                out.append(label + "NACK")
            else:
                out += self.i2c_after(to, label + "ACK", indent, depth)
        return out

    def i2c_after(self, t, head, indent, depth):
        m = self.m
        stop = m.step(t, STOP)
        text = head + ("; STOP: %s" % self.mode(stop[0]) if stop else "")
        reads = self.i2c_starts(t, indent + 4, depth, "repeated ")
        groups = m.grouped(t, m.symbols(t, 0, 256))
        if not groups:
            return [text] + reads
        if len(groups) == 1 and groups[0][0][1] and m.phases[groups[0][0][0]] == ENDED:
            return [text + "; any more bytes: NACK"] + reads
        if depth >= DEEPEST:
            return [text + "; then more bytes: see s%d in the state list" % t] + reads
        return [text + "; then a byte:"] + self.i2c_writes(t, indent + 4, depth + 1) + reads


def account(bus, model):
    """The map, as lines of text."""
    return Mapper(Machine(bus, model)).lines()


def readable(bus, model):
    """The form that reads best: the map, where it shows every byte that matters; the state list, for a
    machine whose bytes matter deeper than the map follows, as a lock's do."""
    lines = account(bus, model)
    if not any("in the state list" in line for line in lines):
        return lines
    return ["%s, %d states learned. Its bytes matter more than %d deep inside a transaction, so it is shown as "
            "its states, one line a state:" % ("SPI mode 0" if bus == "spi" else "I2C", len(model["rows"]),
                                                 DEEPEST)] + states(bus, model)


# ---- the state list and the graph

PHASES = {"spi": ("CS high", "CS low"), "i2c": ("idle", "takes bytes", "sends", "ended")}


def states(bus, model):
    """One line a state: each group of symbols that acts alike, what it answers, and where it leads."""
    m = Machine(bus, model)
    out = []
    for q in range(m.n):
        parts = []
        for (to, answer), symbols, rest in m.grouped(q, m.symbols(q)):
            label = "any other" if rest else m.shown(symbols)
            if m.bus == "spi":
                said = "" if symbols[0] >= 256 else " sends %02X" % answer
            elif symbols[0] in (READ_ACK, READ_NACK):
                said = " reads %02X" % answer
            elif symbols[0] == STOP:
                said = ""
            else:
                said = " NACK" if answer else " ACK"
            parts.append("%s%s -> s%d" % (label, said, to))
        out.append("s%-3d %-11s %s" % (q, PHASES[bus][m.phases[q]], "; ".join(parts)))
    return out


def dot(bus, model, name="device"):
    """The states as a Graphviz digraph, one edge for each pair of states labelled with its symbols. The
    states where the device rests between transactions are drawn twice round."""
    m = Machine(bus, model)
    title = "".join(c if c.isalnum() else "_" for c in name) or "device"
    out = ["digraph %s {" % title, "  rankdir=LR;", "  node [shape=circle];"]
    for q in range(m.n):
        out.append('  s%d [label="s%d"%s];' % (q, q, ", shape=doublecircle" if m.phases[q] == 0 else ""))
    for q in range(m.n):
        edges = OrderedDict()
        for s in m.symbols(q):
            found = m.step(q, s)
            if found is not None:
                edges.setdefault(found[0], []).append(s)
        for to, symbols in edges.items():
            label = m.shown(symbols)
            out.append('  s%d -> s%d [label="%s"];' % (q, to, label if len(label) <= 40 else label[:37] + "..."))
    out.append("}")
    return out
