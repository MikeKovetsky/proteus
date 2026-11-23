# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The baselines: learning from a recording, and questioning at random.

The recording is the traffic a driver written from the device's data sheet would make: every documented
feature in every mode the data sheet gives, its error cases, the shapes the shipped controllers send, and stray
bytes and addresses drawn from all of them, the hidden ones not left out. Resets come between sessions. Two
learners get it.

  passive      blue-fringe state merging for Mealy machines (RPNI and EDSM's family), heaviest state first, best
               merge first. It is helped four times over: it is told the bus phases, that a state sends one byte
               whatever arrives, exactly which symbols the device cares about (the active learner's finding), and
               that every other is its kind's default. The threshold, and whether the same byte sent counts as
               evidence, are whichever of 16 settings did best on a fifth of the recording held out. A setting
               that needs more than 512 states is passed over, and where all do the least cautious is taken.
  states told  a reference, not a learner anyone could run: the true state is told at every step of the
               recording, so no merge can be wrong. It is not a bound, since merging states can guess right
               where this one has seen nothing.

Both complete what was not seen in the same generous way (see Predictor). The third baseline is active, with
random equivalence tests in place of the suites, at the same cost."""

import random
from collections import Counter

from ..host import session
from ..host.bus import Refused
from ..host.learner import Learner
from ..host.search import Random, Search
from . import devices, judge, replace

THRESHOLDS = (0, 1, 2, 4, 8, 16, 32, 64)
SESSION = 20  # transactions between two resets
MOST = 512  # states of a passive model: one with more could not be a program in this chip
MERGED_UP_TO = 700000  # transactions: past this, state merging takes more than 2 GB, and its row is left out


# ---- normal traffic. It is given every documented behaviour, its documented errors and odd shapes included


def undocumented(rng, part):
    """Any byte the data sheet says nothing of, so a hidden one can turn up by luck."""
    while True:
        x = rng.randrange(256)
        if x not in part.documented:
            return x


def flash_driver(rng):
    """One thing a driver would do, as transactions of bytes."""
    doing = rng.choice(("status", "status", "identity", "enable", "disable", "sleep", "wake", "reset", "wrong"))
    dummies = lambda count: [rng.randrange(256) for _ in range(count)]
    if doing == "status":
        return [[0x05] + dummies(rng.randint(0, 4))]
    if doing == "identity":
        return [[0x9F] + dummies(rng.randint(0, 6))]
    if doing == "wrong":
        roll = rng.random()
        if roll < 0.1:
            return [[]]  # a select and nothing else
        if roll < 0.4:
            return [[undocumented(rng, devices.Flash)] + dummies(rng.randint(0, 3))]
        return [[rng.choice((0x04, 0x06, 0x66, 0x99, 0xAB, 0xB9))] + dummies(rng.randint(1, 3))]
    command = {"enable": [0x06], "disable": [0x04], "sleep": [0xB9], "wake": [0xAB]}
    if doing in command:
        out = [command[doing]]
    else:
        out = rng.choice(([[0x66], [0x99]], [[0x66], [0x99]], [[0x66]], [[0x99]]))
    if rng.random() < 0.5:
        out.append([0x05] + dummies(rng.randint(1, 2)))
    return out


def chained(rng, message):
    """I2C transactions, one in six of them several messages joined by repeated STARTs."""
    if rng.random() < 5 / 6:
        return [[message()]]
    return [[message() for _ in range(rng.randint(2, 3))]]


def sensor_driver(rng):
    """Transactions of messages: (address, bytes written or None for a read alone, bytes read)."""
    part = devices.Sensor
    ours, reads = part.ADDRESS, lambda: rng.choice((0, 1, 1, 2, 3, 4, 5))

    def message():
        roll = rng.random()
        if roll < 0.40:
            return (ours, [rng.choice(part.documented)], reads())
        if roll < 0.52:
            return (ours, rng.choice((None, [])), reads() or 1)
        if roll < 0.56:
            return (ours, [], 0)
        if roll < 0.78:
            return (ours, [0x01, rng.choice((0x00, 0x01))] + [undocumented(rng, part)] * (rng.random() < 0.1), reads())
        if roll < 0.84:
            return (ours, [undocumented(rng, part)] + [rng.randrange(2)] * (rng.random() < 0.5), reads())
        if roll < 0.90:
            return (ours, [rng.choice(part.documented), undocumented(rng, part)], reads())
        return (rng.randrange(128), rng.choice((None, [])), rng.randrange(2))

    return chained(rng, message)


def thermometer_driver(rng):
    part = devices.Thermometer

    def message():
        roll = rng.random()
        if roll < 0.45:
            return (0x48, [rng.choice(part.documented)], rng.randint(0, 5))
        if roll < 0.75:
            return (0x48, rng.choice((None, [])), rng.randint(0, 5))
        if roll < 0.87:
            return (0x48, [rng.choice((undocumented(rng, part),) + part.documented),
                           rng.choice((undocumented(rng, part),) + part.documented)][:rng.randint(1, 2)],
                    rng.randint(0, 2))
        return (rng.randrange(128), rng.choice((None, [])), rng.randrange(2))

    return chained(rng, message)


DRIVER = {"flash": flash_driver, "sensor": sensor_driver, "thermometer": thermometer_driver}


def transact(truth, state, transaction):
    """A controller's symbols for one transaction, given up at a NACK. (state, word, answers)"""
    word, out = [], []

    def put(s):
        nonlocal state
        state, answer = truth.step(state, s)
        word.append(s)
        out.append(answer)
        return answer

    if isinstance(truth, judge.I2cTruth):
        for address, written, reads in transaction:
            fine = True
            if written is not None:
                fine = put(judge.START + 2 * address) == 0
                for byte in written:
                    fine = fine and put(byte) == 0
            if fine and reads:
                if put(judge.START + 2 * address + 1) == 0:
                    for i in range(reads):
                        put(judge.READ_NACK if i + 1 == reads else judge.READ_ACK)
        if word:
            put(judge.STOP)
    else:
        for s in [judge.SELECT] + list(transaction) + [judge.DESELECT]:
            put(s)
    return state, word, out


def recording(part, truth, transactions, seed):
    """Sessions from reset, each a list of (word, answers) by transaction."""
    rng, sessions, done = random.Random("%s %d" % (part, seed)), [], 0
    while done < transactions:
        state, now = (0, 0), []
        while len(now) < SESSION and done < transactions:
            for transaction in DRIVER[part](rng):
                state, word, out = transact(truth, state, transaction)
                now.append((word, out))
                done += 1
        sessions.append(now)
    return sessions


def device_test(part, truth):
    """The test written for the device, as symbols: [(word, answers)] in one session."""
    state, out = (0, 0), []
    for _, sent, _ in replace.TESTS[part]:
        if truth.t.bus == "spi":
            transaction = replace.hexes(sent)
        else:
            request = replace.request("i2c", sent)
            transaction = [(request[0], request[2:-1], request[-1])]  # the controller always addresses to write
        state, word, answers = transact(truth, state, transaction)
        out.append((word, answers))
    return out


# ---- learning from it

class View:
    """What both passive learners are told of the bus and the alphabet."""

    def __init__(self, truth, model):
        self.truth = truth
        self.known = set(model["alphabet"])
        self.defaults = model["defaults"]
        self.i2c = isinstance(truth, judge.I2cTruth)

    def fold(self, s):
        return s if s in self.known else self.defaults[self.truth.kind(s)]

    def after(self, phase, s, out):
        if not self.i2c:
            return judge.LOW if s == judge.SELECT or s < 256 else judge.HIGH
        if s == judge.STOP:
            return judge.IDLE
        if s == judge.READ_ACK:
            return judge.SENDS
        if s == judge.READ_NACK or out:
            return judge.OVER
        return judge.SENDS if s >= judge.START and s & 1 else judge.TAKES

    def sends(self, phase, s):
        """Is the answer the state's own byte?"""
        return phase == judge.SENDS if self.i2c else s < 256


class Merger:
    def __init__(self, view, sessions, bytes_count=True):
        self.view, self.bytes_count = view, bytes_count
        self.kids, self.phase, self.byte, self.weight, self.into = [], [], [], [], []
        self.node(0)
        for transactions in sessions:
            at = 0
            for word, out in transactions:
                for s, answer in zip(word, out):
                    self.weight[at] += 1
                    if view.sends(self.phase[at], s):
                        self.byte[at] = answer
                    s = view.fold(s)
                    if s not in self.kids[at]:
                        self.kids[at][s] = (self.node(view.after(self.phase[at], s, answer)), answer)
                    at = self.kids[at][s][0]
            self.weight[at] += 1

    def node(self, phase):
        self.kids.append({})
        self.phase.append(phase)
        self.byte.append(None)
        self.weight.append(0)
        self.into.append(None)
        return len(self.kids) - 1

    def top(self, at):
        while self.into[at] is not None:
            at = self.into[at]
        return at

    def merge(self, red, blue):
        """(score, what undoes it), or None where they disagree."""
        todo, undo, score = [(red, blue)], [], 0
        while todo:
            a, b = todo.pop()
            a, b = self.top(a), self.top(b)
            if a == b:
                continue
            mine, theirs = self.byte[a], self.byte[b]
            if self.bytes_count and mine is not None and mine == theirs:
                score += 1  # the same byte sent is evidence too
            if self.phase[a] != self.phase[b] or (mine is not None and theirs is not None and mine != theirs):
                self.back(undo)
                return None
            self.into[b] = a
            undo.append((0, b, None))
            undo.append((1, a, self.weight[a]))
            self.weight[a] += self.weight[b]
            if mine is None and theirs is not None:
                undo.append((2, a, None))
                self.byte[a] = theirs
            for s, (to, out) in self.kids[b].items():
                have = self.kids[a].get(s)
                if have is None:
                    self.kids[a][s] = (to, out)
                    undo.append((3, a, s))
                elif have[1] != out:
                    self.back(undo)
                    return None
                else:
                    score += 1
                    todo.append((have[0], to))
        return score, undo

    def back(self, undo):
        for what, at, old in reversed(undo):
            if what == 0:
                self.into[at] = None
            elif what == 1:
                self.weight[at] = old
            elif what == 2:
                self.byte[at] = None
            else:
                del self.kids[at][old]

    def run(self, threshold, most=None):
        """The machine, or None once it needs more than `most` states."""
        red, is_red = [0], {0}
        while True:
            blue = {}
            for q in red:
                for s, (to, _) in self.kids[q].items():
                    to = self.top(to)
                    if to not in is_red:
                        blue[to] = True
            if not blue:
                break
            chosen = max(blue, key=lambda b: (self.weight[b], -b))
            best = None
            for q in red:
                if self.phase[q] != self.phase[chosen]:
                    continue
                found = self.merge(q, chosen)
                if found:
                    self.back(found[1])
                    if found[0] >= threshold and (best is None or found[0] > best[0]):
                        best = (found[0], q)
            if best:
                self.merge(best[1], chosen)
            else:
                red.append(chosen)
                is_red.add(chosen)
                if most and len(red) > most:
                    return None
        number = {q: i for i, q in enumerate(red)}
        rows = [{s: (number[self.top(to)], out) for s, (to, out) in self.kids[q].items()} for q in red]
        return Predictor(self.view, rows, [self.byte[q] for q in red], [self.phase[q] for q in red])


STAY = -1


class Predictor:
    """A machine from a recording, completed the way that helps it, and never lost. What a state was not seen
    to do with a symbol is guessed in this order: what it does with that kind's default, what that symbol does
    in most states of that phase, what most symbols of its kind do there, what that kind does in most states of
    that phase. A state's own byte, once seen, is what it sends."""

    def __init__(self, view, rows, bytes_, phases):
        self.view, self.rows, self.bytes, self.phases = view, rows, bytes_, phases
        self.filled = [{} for _ in rows]
        self.home = {}
        self.by_symbol, self.by_kind = {}, {}
        for q, row in enumerate(rows):
            self.home.setdefault(phases[q], q)
            for s, (to, out) in row.items():
                what = (STAY if to == q else to, out)
                kind = view.truth.kind(s)
                self.by_symbol.setdefault((phases[q], s), Counter())[what] += 1
                if kind is not None:
                    self.by_kind.setdefault((phases[q], kind), Counter())[what] += 1

    def step(self, q, phase, s):
        folded = self.view.fold(s)
        found = self.rows[q].get(folded)
        if found:
            return found
        if (phase, folded) not in self.filled[q]:
            self.filled[q][(phase, folded)] = self.guess(q, phase, folded)
        return self.filled[q][(phase, folded)]

    def guess(self, q, phase, s):
        view, row = self.view, self.rows[q]
        kind, found = view.truth.kind(s), None
        tallies = []
        if kind is not None:
            found = row.get(view.defaults[kind])
        if not found:
            tallies.append(self.by_symbol.get((self.phases[q], s)))
            if kind is not None:
                tallies.append(Counter(what for other, what in row.items() if view.truth.kind(other) == kind))
                tallies.append(self.by_kind.get((self.phases[q], kind)))
            found = next((tally.most_common(1)[0][0] for tally in tallies if tally), (None, None))
        to, out = found
        if view.sends(phase, s):
            out = out if self.bytes[q] is None else self.bytes[q]
        elif out is None:
            out = 0
        after = view.after(phase, s, out)
        if to == STAY:
            to = q
        if to is None:
            to = q if self.phases[q] == after else self.home.get(after, q)
        return to, out


def states_told(view, minimal, sessions):
    """Told the true state at each step of the recording."""
    rows, sent = [{} for _ in range(minimal.n)], [None] * minimal.n
    for transactions in sessions:
        q = minimal.start
        for word, out in transactions:
            for s, answer in zip(word, out):
                if view.sends(minimal.phase[q], s):
                    sent[q] = answer
                to = minimal.rows[q][s][0]
                rows[q][view.fold(s)] = (to, answer)
                q = to
    order = [minimal.start] + [q for q in range(minimal.n) if q != minimal.start]
    number = {q: i for i, q in enumerate(order)}
    learned = [{s: (number[to], out) for s, (to, out) in rows[q].items()} for q in order]
    return Predictor(view, learned, [sent[q] for q in order], [minimal.phase[q] for q in order])


def passive(view, sessions):
    cut = max(1, len(sessions) * 4 // 5)
    ranked = []
    for bytes_count in (False, True):
        for threshold in THRESHOLDS:
            found = Merger(view, sessions[:cut], bytes_count).run(threshold, MOST)
            if found is None:
                break  # a higher threshold merges less, so it needs more states still
            right = sum(scored(found, sessions[cut:] or sessions))
            ranked.append((-right, len(found.rows), threshold, bytes_count))
    for _, _, threshold, bytes_count in sorted(ranked):  # the best of those that fit on all of the recording too
        learned = Merger(view, sessions, bytes_count).run(threshold, MOST)
        if learned:
            learned.threshold = threshold
            return learned
    learned = Merger(view, sessions, False).run(0)
    learned.threshold = 0
    return learned


def scored(predictor, sessions):
    """Was each transaction answered right, each session taken from reset?"""
    out = []
    for transactions in sessions:
        q, phase = 0, 0
        for word, answers in transactions:
            right = True
            for s, answer in zip(word, answers):
                q, said = predictor.step(q, phase, s)
                right = right and said == answer
                phase = predictor.view.after(phase, s, answer)
            out.append(right)
    return out


def exact(predictor, minimal):
    start = (minimal.start, 0)
    seen, todo = {start}, [start]
    while todo:
        p, q = todo.pop()
        for s, (to, out) in minimal.rows[p].items():
            at, said = predictor.step(q, minimal.phase[p], s)
            if said != out:
                return False
            if (to, at) not in seen:
                seen.add((to, at))
                todo.append((to, at))
    return True


def model_predictor(view, model):
    rows = [{s: (to, out) for s, to, out in row} for row in model["rows"]]
    return Predictor(view, rows, [None] * len(rows), model["phases"])


def measured(part, predictor, minimal, unseen, test, needs):
    results = scored(predictor, [test])
    return {"states": len(predictor.rows),
            "normal traffic": sum(scored(predictor, unseen)) / sum(len(one) for one in unseen),
            "device test": (sum(results), len(results)),
            "hidden features": (sum(results[i] for i in needs), len(needs)),
            "exact": exact(predictor, minimal)}


def needs_hidden(part, test):
    """The lines of the test that a device built to its data sheet alone gets wrong."""
    part_ = devices.AS_DOCUMENTED[part]()
    table = devices.spi_table(part_) if replace.BUS[part] == "spi" else devices.i2c_table(part_)
    plain = device_test(part, judge.TRUTH[table.bus](table))
    return [i for i, (want, have) in enumerate(zip(test, plain)) if want != have]


def as_long(part, table, symbols, seed=1):
    """How many transactions of the recording come to that many symbols."""
    sessions = recording(part, judge.TRUTH[table.bus](table), 1000, seed)
    return max(1, int(symbols * 1000 // sum(len(word) for one in sessions for word, _ in one)))


def compared(part, table, model, lengths, seed=1, merged_up_to=MERGED_UP_TO):
    """Each learner on the same device: {"active": ..., (how many transactions, learner): ...}."""
    truth = judge.TRUTH[table.bus](table)
    minimal = judge.Minimal(truth)
    view = View(truth, model)
    unseen = recording(part, truth, 1000, seed + 1000)
    test = device_test(part, truth)
    needs = needs_hidden(part, test)
    out = {"active": measured(part, model_predictor(view, model), minimal, unseen, test, needs)}
    for length in lengths:
        sessions = recording(part, truth, length, seed)
        spent = sum(len(word) for one in sessions for word, _ in one)
        if length <= merged_up_to:
            learned = passive(view, sessions)
            out[(length, "passive")] = dict(measured(part, learned, minimal, unseen, test, needs),
                                            threshold=learned.threshold, symbols=spent)
        out[(length, "states told")] = dict(
            measured(part, states_told(view, minimal, sessions), minimal, unseen, test, needs), symbols=spent)
    return out


# ---- questioning at random

class RandomSearch(Search):
    """Random words from reset, four symbols in five from those learned so far, until the symbols run out."""

    def __init__(self, learner, spend):
        Search.__init__(self, learner)
        self.spend = spend
        self.random = Random(self.told["seed"] + 77)

    def find(self, model):
        self.prepare(model)
        self.checked = 0
        draw = self.random.below
        while self.learner.symbols < self.spend:
            q, word = 0, []
            for _ in range(1 + draw(24)):
                phase, options = model.phases[q], self.order[q]
                if options and draw(5):
                    s = options[draw(len(options))]
                else:
                    s = self.bus.nth(phase, draw(self.bus.count(phase)))
                word.append(s)
                q = model.step(q, s)[0]
            found = self.test(model, 0, tuple(word))
            if found:
                return found
        return None


class Direct:
    """The link to a board, with no jail: for the baseline only."""

    def __init__(self, on):
        self.on = on

    def load(self, image):
        return self.on.load(image)

    def exchange(self, data, wanted):
        return self.on.exchange(data, wanted)


def questioned_at_random(part, table, on, told, spend):
    """The baseline, scored as `compared` scores the others, and exact only by the judge."""
    found = at_random(on, told, spend)
    if not found["model"]:
        return found
    truth = judge.TRUTH[table.bus](table)
    minimal = judge.Minimal(truth)
    test = device_test(part, truth)
    predictor = model_predictor(View(truth, found["model"]), found["model"])
    out = measured(part, predictor, minimal, recording(part, truth, 1000, 1001), test, needs_hidden(part, test))
    out.update(exact=judge.hypothesis(truth, minimal, found["model"])["equal"], symbols=found["symbols"])
    return out


def at_random(on, told, spend):
    """The active learner, with random equivalence tests and nothing else, for `spend` symbols."""
    told = session.settings(dict(told, budget=max(told.get("budget", 6000000), 2 * spend)))
    bus = session.BUSES[told["bus"]](told)
    link = Direct(on)
    link.load(bus.image())
    learner = Learner(bus, link, told)
    try:
        model = learner.learn(RandomSearch(learner, spend))
    except Refused as refusal:
        return {"model": None, "reason": refusal.text, "symbols": learner.symbols}
    return {"model": session.exported(model, bus), "reason": "", "symbols": learner.symbols}
