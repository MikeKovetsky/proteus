# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Seeded families of devices that the host was not tuned on.

The host was frozen at FREEZE, and every device's seed comes from that commit's hash, so neither could be picked
to suit the other. This file was written after the freeze and committed before the host saw any of its devices.
Each bus has five families:

  registers   a pointer or command chooses a register; widths, banks and auto-increment are drawn
  modes       commands move between modes, at once or only when the transaction ends as it should
  traced      a key of two to five transactions whose progress can be read
  untraced    the same with nothing to read before the last step: outside what any bound here promises
  fields      whole ranges of opcodes or addresses act alike, against the host's one default for each kind

Nothing is dropped: each refusal, miss, failure and crash is counted.

The first blind run is learn/results/families.jsonl, by the host frozen at FREEZE. Its devices have been seen
since, so they are now a development set: `evaluate` runs any host on them, says which, and `--host first`
runs the first freeze's host again, taken from git and checked against FROZEN_DIGEST. A blind run needs devices
seeded from a later freeze."""

import hashlib
import io
import json
import random
import subprocess
import tarfile
import traceback
from collections import Counter, OrderedDict
from multiprocessing import Pool
from pathlib import Path

from . import board, devices, jail, judge

FREEZE = "a0acf17d6af17cb9a0ed1e4de08bd01d13a6204b"
COUNT = 16  # devices in each family
ROOT = Path(__file__).resolve().parents[2]
FROZEN = ("learn/host", "learn/programs")
FROZEN_DIGEST = "a69ea9acc420426f6d960ca785dba9b8af44bafc219916988d3f84d31d53a609"  # of those files at FREEZE


def some_bytes(rng, count):
    return rng.sample(range(256), count)


def values(rng, least=1, most=3):
    return tuple(rng.randrange(256) for _ in range(rng.randint(least, most)))


def at(data, i, beyond=0xFF):
    return data[i] if i < len(data) else beyond


# ---- SPI. A state is (what lasts, what the transaction is doing)

class Spi:
    name = "spi"
    idle = 0xFF  # what it sends when it has nothing to say

    def reset(self):
        return self.first, ("off",)

    def select(self, state):
        return state[0], ("op",)

    def deselect(self, state):
        return self.ended(*state), ("off",)

    def ended(self, kept, doing):
        return kept

    def sends(self, state):
        kept, doing = state
        if doing[0] != "read":
            return self.idle
        return at(self.data(kept, doing[1]), doing[2], self.beyond)

    def byte(self, state, x):
        kept, doing = state
        if doing[0] == "op":
            return self.command(kept, x)
        if doing[0] == "read":
            return self.onward(kept, doing[1], doing[2])
        return self.more(kept, doing, x)

    def onward(self, kept, what, i):
        return kept, ("read", what, min(i + 1, len(self.data(kept, what))))

    def more(self, kept, doing, x):
        return kept, ("ignore",)


class SpiRegisters(Spi):
    def __init__(self, rng):
        self.idle = rng.choice((0x00, 0xFF))
        self.beyond = rng.choice((0x00, 0xFF))
        count, self.banks = rng.randint(2, 5), rng.randint(1, 3)
        codes = some_bytes(rng, count + 1 + self.banks)
        self.ops, self.switch, self.picks = codes[:count], codes[count], codes[count + 1:]
        self.store = [[values(rng) for _ in range(count)] for _ in range(self.banks)]
        self.increments = rng.random() < 0.5
        self.first = 0

    def data(self, kept, what):
        return self.store[kept][what]

    def onward(self, kept, what, i):
        if self.increments and i + 1 >= len(self.data(kept, what)) and what + 1 < len(self.ops):
            return kept, ("read", what + 1, 0)
        return Spi.onward(self, kept, what, i)

    def command(self, kept, x):
        if x in self.ops:
            return kept, ("read", self.ops.index(x), 0)
        if self.banks > 1 and x == self.switch:
            return kept, ("bank",)
        return kept, ("ignore",)

    def more(self, kept, doing, x):
        if doing[0] == "bank" and x in self.picks:
            return self.picks.index(x), ("ignore",)
        return kept, ("ignore",)


class SpiModes(Spi):
    def __init__(self, rng):
        self.idle = rng.choice((0x00, 0xFF, rng.randrange(256)))
        self.beyond = self.idle
        modes, orders, reads = rng.randint(2, 5), rng.randint(2, 5), rng.randint(1, 3)
        codes = some_bytes(rng, orders + reads)
        self.orders, self.reads = codes[:orders], codes[orders:]
        self.moves = [[rng.randrange(modes) if rng.random() < 0.6 else q for _ in self.orders] for q in range(modes)]
        self.store = [[values(rng, 1, 2) for _ in self.reads] for _ in range(modes)]
        self.at_end = rng.random() < 0.5
        self.first = 0

    def data(self, kept, what):
        return self.store[kept][what]

    def command(self, kept, x):
        if x in self.reads:
            return kept, ("read", self.reads.index(x), 0)
        if x in self.orders:
            order = self.orders.index(x)
            return (kept, ("order", order)) if self.at_end else (self.moves[kept][order], ("ignore",))
        return kept, ("ignore",)

    def ended(self, kept, doing):
        return self.moves[kept][doing[1]] if doing[0] == "order" else kept


class SpiTraced(Spi):
    traced = True

    def __init__(self, rng):
        self.beyond = rng.choice((0x00, 0xFF))
        steps = rng.randint(2, 5)
        self.identity, self.status, self.secret, *codes = some_bytes(rng, steps + 3)
        self.key = [rng.choice(codes) for _ in range(steps)]
        self.plain, self.hidden = values(rng), values(rng)
        self.first = 0

    def data(self, kept, what):
        return {"identity": self.plain, "status": (kept,), "secret": self.hidden}[what]

    def command(self, kept, x):
        open_ = kept == len(self.key)
        if x == self.identity:
            return kept, ("read", "identity", 0)
        if x == self.status and self.traced:
            return kept, ("read", "status", 0)
        if x == self.secret and open_:
            return kept, ("read", "secret", 0)
        if open_:
            return kept, ("ignore",)
        if x == self.key[kept]:
            return kept + 1, ("ignore",)
        return (1 if x == self.key[0] else 0), ("ignore",)


class SpiUntraced(SpiTraced):
    traced = False


class SpiFields(Spi):
    def __init__(self, rng):
        self.beyond = rng.choice((0x00, 0xFF))
        self.free = rng.randint(1, 4)  # the low bits of the opcode that are no part of it
        groups = rng.sample(range(256 >> self.free), rng.randint(2, 4))
        self.change, self.reads = groups[0], groups[1:]
        self.store = [[values(rng) for _ in self.reads] for _ in range(2)]
        self.first = 0

    def data(self, kept, what):
        return self.store[kept][what]

    def command(self, kept, x):
        group = x >> self.free
        if group == self.change:
            return 1 - kept, ("ignore",)
        if group in self.reads:
            return kept, ("read", self.reads.index(group), 0)
        return kept, ("ignore",)


# ---- I2C. A state is (what lasts, what the message is doing)

class I2c:
    name = "i2c"

    def __init__(self, rng):
        self.address = rng.randint(0x08, 0x77)
        self.first = 0

    def reset(self):
        return self.first, ("none",)

    def ours(self, kept, address):
        return address == self.address

    def start(self, state, x):
        kept = self.ended(*state, stopped=False)
        if not self.ours(kept, x >> 1):
            return (kept, ("none",)), False
        if x & 1:
            return (kept, ("read", self.reading(kept, x >> 1), 0)), True
        return (kept, ("op", x >> 1 if self.addressed else 0)), True

    addressed = False  # does it care which of its addresses a write came to?

    def reading(self, kept, address):
        return 0

    def write(self, state, x):
        kept, doing = state
        if doing[0] == "op":
            return self.command(kept, doing[1], x)
        return self.more(kept, doing, x)

    def more(self, kept, doing, x):
        return (kept, ("none",)), False

    def sends(self, state):
        kept, doing = state
        return at(self.data(kept, doing[1]), doing[2]) if doing[0] == "read" else 0xFF

    def read(self, state, nack):
        kept, doing = state
        if nack or doing[0] != "read":
            return kept, ("none",)
        return self.onward(kept, doing[1], doing[2])

    def onward(self, kept, what, i):
        return kept, ("read", what, min(i + 1, len(self.data(kept, what))))

    def stop(self, state):
        return self.ended(*state, stopped=True), ("none",)

    def ended(self, kept, doing, stopped):
        return kept


class I2cRegisters(I2c):
    def __init__(self, rng):
        I2c.__init__(self, rng)
        count, settings = rng.randint(2, 4), rng.randint(1, 3)
        self.pointers = some_bytes(rng, count)
        self.settings = some_bytes(rng, settings)
        self.control = rng.randrange(count)
        self.store = [[values(rng, 1, 2) for _ in range(count)] for _ in range(settings)]
        self.increments = rng.random() < 0.4
        self.forgets = rng.random() < 0.3  # a STOP puts the pointer back
        self.first = (0, 0)

    def data(self, kept, what):
        return self.store[kept[0]][what]

    def reading(self, kept, address):
        return kept[1]

    def onward(self, kept, what, i):
        if self.increments and i + 1 >= len(self.data(kept, what)) and what + 1 < len(self.pointers):
            return kept, ("read", what + 1, 0)
        return I2c.onward(self, kept, what, i)

    def command(self, kept, address, x):
        if x not in self.pointers:
            return (kept, ("none",)), False
        pointer = self.pointers.index(x)
        return ((kept[0], pointer), ("value",) if pointer == self.control else ("taken",)), True

    def more(self, kept, doing, x):
        if doing[0] == "value" and len(self.settings) > 1 and x in self.settings:
            return ((self.settings.index(x), kept[1]), ("taken",)), True
        return (kept, ("none",)), False

    def ended(self, kept, doing, stopped):
        return (kept[0], 0) if stopped and self.forgets else kept


class I2cModes(I2c):
    def __init__(self, rng):
        I2c.__init__(self, rng)
        modes = rng.randint(2, 5)
        self.orders = some_bytes(rng, rng.randint(2, 5))
        self.moves = [[rng.randrange(modes) if rng.random() < 0.6 else q for _ in self.orders] for q in range(modes)]
        self.store = [values(rng) for _ in range(modes)]
        self.deaf = [q > 0 and rng.random() < 0.2 for q in range(modes)]  # modes that don't answer a read
        self.at_end = rng.random() < 0.5

    def data(self, kept, what):
        return self.store[kept]

    def start(self, state, x):
        (kept, doing), ack = I2c.start(self, state, x)
        if ack and x & 1 and self.deaf[kept]:
            return (kept, ("none",)), False
        return (kept, doing), ack

    def command(self, kept, address, x):
        if x not in self.orders:
            return (kept, ("none",)), False
        order = self.orders.index(x)
        if self.at_end:
            return (kept, ("order", order)), True
        return (self.moves[kept][order], ("taken",)), True

    def ended(self, kept, doing, stopped):
        return self.moves[kept][doing[1]] if stopped and doing[0] == "order" else kept


class I2cTraced(I2c):
    traced = True
    addressed = True

    def __init__(self, rng):
        I2c.__init__(self, rng)
        self.other = rng.choice([a for a in range(0x08, 0x78) if a != self.address])
        steps = rng.randint(2, 4)
        codes = some_bytes(rng, steps)
        self.key = [rng.choice(codes) for _ in range(steps)]
        self.plain, self.hidden = values(rng), values(rng)

    def ours(self, kept, address):
        return address == self.address or (address == self.other and kept == len(self.key))

    def data(self, kept, what):
        if what:
            return self.hidden
        return (kept,) + self.plain if self.traced else self.plain

    def reading(self, kept, address):
        return 1 if address == self.other else 0

    def command(self, kept, address, x):
        if address != self.address or kept == len(self.key):
            return (kept, ("taken",)), True
        if x == self.key[kept]:
            return (kept + 1, ("taken",)), True
        return (1 if x == self.key[0] else 0, ("taken",)), True

    def more(self, kept, doing, x):
        return (kept, ("taken",)), True


class I2cUntraced(I2cTraced):
    traced = False


class I2cFields(I2c):
    def __init__(self, rng):
        I2c.__init__(self, rng)
        self.pins = rng.randint(1, 3)  # low address bits it doesn't look at
        self.free = rng.randint(1, 4)  # low opcode bits likewise
        self.groups = rng.sample(range(256 >> self.free), rng.randint(2, 3))
        self.store = [values(rng) for _ in range(len(self.groups) + 1)]

    def ours(self, kept, address):
        return address >> self.pins == self.address >> self.pins

    def data(self, kept, what):
        return self.store[kept]

    def command(self, kept, address, x):
        group = x >> self.free
        if group not in self.groups:
            return (kept, ("none",)), False
        return (self.groups.index(group) + 1, ("taken",)), True


FAMILIES = OrderedDict((
    ("spi registers", SpiRegisters), ("spi modes", SpiModes), ("spi traced", SpiTraced),
    ("spi untraced", SpiUntraced), ("spi fields", SpiFields),
    ("i2c registers", I2cRegisters), ("i2c modes", I2cModes), ("i2c traced", I2cTraced),
    ("i2c untraced", I2cUntraced), ("i2c fields", I2cFields),
))


def seed(family, i):
    return int(hashlib.sha256(("%s %s %d" % (FREEZE, family, i)).encode("ascii")).hexdigest()[:15], 16)


def device(family, i):
    part = FAMILIES[family](random.Random(seed(family, i)))
    table = (devices.spi_table if part.name == "spi" else devices.i2c_table)(part)
    table.name = "%s #%d" % (family, i)
    return table


def digest(root=ROOT):
    """Of every source under the frozen folders, by name and content."""
    root = Path(root)
    names = sorted(path.relative_to(root).as_posix() for folder in FROZEN for path in (root / folder).rglob("*")
                   if path.is_file() and path.suffix in (".py", ".fsm"))
    whole = hashlib.sha256()
    for name in names:
        whole.update(name.encode("utf-8") + b"\0" + hashlib.sha256((root / name).read_bytes()).digest())
    return whole.hexdigest()


def frozen(root=ROOT):
    """Is the host under root the host of the first freeze?"""
    return digest(root) == FROZEN_DIGEST


def which(root=ROOT):
    """The freeze whose host is under root, or None for a host that is no freeze."""
    found = digest(root)
    return {FROZEN_DIGEST: "first freeze"}.get(found)


def first_host(into):
    """The first freeze's host, out of git into the folder `into`, checked against its digest. Its learn/host."""
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", FREEZE] + list(FROZEN), check=True,
                             stdout=subprocess.PIPE).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        try:
            tar.extractall(str(into), filter="data")
        except TypeError:  # a Python without extraction filters
            tar.extractall(str(into))
    if not frozen(into):
        raise RuntimeError("the first freeze's host from git does not match its digest")
    return Path(into) / "learn/host"


def one(task):
    """One device learned in the jail and judged, by the host in `source`. Whatever goes wrong is a row, and a
    failure."""
    family, i = task[:2]
    source = task[2] if len(task) > 2 else jail.HOST
    row = {"family": family, "i": i}
    try:
        table = device(family, i)
        run_seed = seed(family, i) % 100000 + 1
        with board.Board(table.bus, run_seed) as on:
            on.plug(table)
            result = jail.learn(on, {"bus": table.bus, "seed": run_seed}, source=source)
            report = judge.judge(on, table, result)
        stats = result.get("stats") or {}
        row.update(verdict=report["verdict"], code=result["code"], reason=result["reason"],
                   true_states=report["true_states"], states=report.get("learned_states"),
                   inside=report.get("inside"), uniform=report.get("uniform"),
                   entries=stats.get("entries"), chain=stats.get("chain"), symbols=stats.get("symbols"),
                   queries=stats.get("queries"),
                   flags=result.get("flags", []), found_by=stats.get("found_by"), pairs=stats.get("pairs"),
                   chip_ms=result.get("chip_ms"), wall_s=result.get("wall_s"))
        wrong = [found for found in [report.get("model")] + report["clone"] if found and not found["equal"]]
        if wrong:
            row["witness"] = [int(s) for s in wrong[0].get("witness") or [] if s is not None]
            row["fault"] = wrong[0].get("fault", "")
    except Exception:
        row.update(verdict=judge.ERROR, code="harness", reason=traceback.format_exc())
    return row


def tasks(count=COUNT):
    """Every device, the families interleaved, so that a run cut short has some of each."""
    return [(family, i) for i in range(count) for family in FAMILIES]


def evaluate(out=None, count=COUNT, workers=None, say=print, source=jail.HOST):
    board.library()  # built once, before there are several to build it
    rows = []
    with Pool(workers or board.jobs()) as pool:
        for row in pool.imap_unordered(one, [task + (str(source),) for task in tasks(count)]):
            rows.append(row)
            if out:
                with open(str(out), "a") as log:
                    log.write(json.dumps(row, sort_keys=True) + "\n")
            say("%3d/%d  %-14s #%-2d %-18s %s" % (len(rows), count * len(FAMILIES), row["family"], row["i"],
                                                 row["verdict"], "" if row["code"] == "ok" else row["code"]))
    return rows


def span(numbers):
    numbers = [n for n in numbers if n is not None]
    if not numbers:
        return "-"
    return "%d" % min(numbers) if min(numbers) == max(numbers) else "%d-%d" % (min(numbers), max(numbers))


def flagged(rows):
    """Per flag, how many copies carry it and how many of those are exact or wrong. A copy is a row with a
    program; wrong is missed or failed."""
    copies = [row for row in rows if row["verdict"] != judge.REFUSED and row["code"] == "ok"]
    names = sorted(set(flag for row in copies for flag in row.get("flags") or []))
    lines = ["%-26s %6s %6s %6s" % ("warning", "copies", "exact", "wrong")]
    for name in ["every copy"] + names + ["no flag"]:
        chosen = [row for row in copies if name == "every copy" or name in (row.get("flags") or []) or
                  (name == "no flag" and not row.get("flags"))]
        exact = sum(row["verdict"] == judge.EXACT for row in chosen)
        lines.append("%-26s %6d %6d %6d" % (name, len(chosen), exact, len(chosen) - exact))
    asked = [row["queries"] for row in rows if row.get("queries") is not None]
    if asked:
        lines.append("questions per device: %d on average, %d at most, %d in all" % (
            sum(asked) // len(asked), max(asked), sum(asked)))
    return lines


def summary(rows):
    """A line a family, a total, and every reason that was not a copy."""
    lines = ["%-14s %7s %6s %8s %8s %8s  %-11s %s" % ("family", "devices", "exact", "refused", "missed", "FAILED",
                                                     "true states", "entries of those exact")]
    for family in list(FAMILIES) + ["all"]:
        chosen = [row for row in rows if family in ("all", row["family"])]
        verdicts = Counter(row["verdict"] for row in chosen)
        lines.append("%-14s %7d %6d %8d %8d %8d  %-11s %s" % (
            family, len(chosen), verdicts[judge.EXACT], verdicts[judge.REFUSED], verdicts[judge.MISSED],
            sum(verdicts[failure] for failure in judge.FAILURES), span(row.get("true_states") for row in chosen),
            span(row.get("entries") for row in chosen if row["verdict"] == judge.EXACT)))
    lines += flagged(rows)
    refusals = Counter((row["family"], row["code"]) for row in rows if row["verdict"] == judge.REFUSED)
    for (family, code), count in sorted(refusals.items()):
        lines.append("refused: %-14s %2d  %s" % (family, count, code))
    for row in rows:
        if row["verdict"] in judge.FAILURES:
            lines.append("%s: %s #%d: %s" % (row["verdict"], row["family"], row["i"], row["reason"]))
    return lines
