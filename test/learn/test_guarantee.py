# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The stated guarantee, tested by exhaustion on small machines: each suite alone finds every machine within
its bound that differs from the model. And each stops doing so when the suite is weakened."""

import itertools
import random
import unittest

import common  # noqa: F401
from learn.host.model import Model
from learn.host.search import Search


class Toy:
    """Two symbols and one phase, or two phases that follow from the answers as I2C's do from an ACK."""

    def __init__(self, phases=1):
        self.inputs = None
        self.allowed_in = ((0, 1), (1,))[:phases]
        self.phases = phases

    def allows(self, phase, s):
        return s in self.allowed_in[phase]

    def count(self, phase):
        return len(self.allowed_in[phase])

    def nth(self, phase, i):
        return self.allowed_in[phase][i]

    def after(self, phase, s, out):
        return out if self.phases == 2 else 0

    def kind(self, s):
        return None


def machines(states):
    """Every Mealy machine over two symbols with two answers and that many states."""
    cells = [(to, out) for to in range(states) for out in (0, 1)]
    for flat in itertools.product(cells, repeat=2 * states):
        yield [flat[2 * q:2 * q + 2] for q in range(states)]


def product(bus, machine):
    """The machine as seen on the bus: (phases, rows, access words, their answers), from state 0 in phase 0."""
    order, index = [(0, 0)], {(0, 0): 0}
    phases, rows, access, answers = [], [], [()], [()]
    for at, (q, phase) in enumerate(order):
        row = {}
        for s in bus.allowed_in[phase]:
            to, out = machine[q][s]
            after = (to, bus.after(phase, s, out))
            if after not in index:
                index[after] = len(order)
                order.append(after)
                access.append(access[at] + (s,))
                answers.append(answers[at] + (out,))
            row[s] = (index[after], out)
        phases.append(phase)
        rows.append(row)
    return phases, rows, access, answers


def model_of(bus, machine):
    phases, rows, access, answers = product(bus, machine)
    model = Model(bus, phases, rows, [0, 1], [], access, answers)
    return model if model.minimal() else None


class Implementation:
    """The learner as far as a search needs one: a way to ask."""

    def __init__(self, bus):
        self.bus = bus
        self.told = {"seed": 1, "extra": 0, "max_states": None, "effort": 0, "budget": 10 ** 12}
        self.symbols = 0
        self.machine = None

    def ask(self, word, pauses=(), pause_us=0):
        out, q, phase, legal = [], 0, 0, True
        for s in word:
            legal = legal and self.bus.allows(phase, s)
            if legal:
                q, answer = self.machine[q][s]
                phase = self.bus.after(phase, s, answer)
            else:
                answer = -1
            out.append(answer)
        return tuple(out)


def equivalent(bus, model, machine):
    seen, todo = set(), [(0, 0)]
    while todo:
        pair = todo.pop()
        if pair not in seen:
            seen.add(pair)
            q, p = pair
            for s, (to, out) in model.rows[q].items():
                at, answer = machine[p][s]
                if answer != out:
                    return False
                todo.append((to, at))
    return True


def escapes(bus, model, candidates, suite, extra=0, weaken=None):
    """How many differing machines the suite lets through, and how many it was shown."""
    learner = Implementation(bus)
    search = Search(learner)
    search.prepare(model)
    search.choose_depth = lambda _: 0  # the cost of a suite is tested on its own
    search.extra = extra
    if weaken:
        weaken(search)
    run, missed, shown = getattr(search, suite), 0, 0
    for machine in candidates:
        shown += 1
        learner.machine = machine
        search.sweep_from, search.depth_from = (0, 0), 0
        if run(model) is None and not equivalent(bus, model, machine):
            missed += 1
    return missed, shown


def no_w(search):
    for phase in search.whole:
        search.whole[phase] = [()]
    search.identify = [[()] for _ in search.identify]


def no_w_i(search):
    search.identify = [[()] for _ in search.identify]


def one_less(search):
    search.extra -= 1


class Guarantee(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.three = list(machines(3))
        rng = random.Random(5)
        cls.models = {}
        for size, count in ((1, 4), (2, 6), (3, 6)):
            pool = list(machines(size))
            rng.shuffle(pool)
            found = []
            for machine in pool:
                model = model_of(Toy(), machine)
                if model and model.n == size:
                    found.append(model)
                    if len(found) == count:
                        break
            cls.models[size] = found

    def test_sweep_finds_every_fault_within_n_states(self):
        for size, candidates in ((2, list(machines(2)) + list(machines(1))), (3, self.three)):
            for model in self.models[size]:
                missed, shown = escapes(Toy(), model, candidates, "sweep")
                self.assertEqual(missed, 0, "n=%d: %d of %d got through" % (size, missed, shown))

    def test_depth_finds_every_fault_within_n_plus_m_states(self):
        for size in (1, 2, 3):
            for model in self.models[size]:
                missed, shown = escapes(Toy(), model, self.three, "depth", extra=3 - size)
                self.assertEqual(missed, 0, "n=%d, m=%d: %d of %d got through" % (size, 3 - size, missed, shown))

    def test_a_weakened_suite_lets_faults_through(self):
        for suite, size, extra, weaken in (
            ("sweep", 3, 0, no_w_i), ("sweep", 3, 0, no_w), ("depth", 2, 1, one_less), ("depth", 3, 0, no_w_i),
            ("depth", 1, 2, one_less),
        ):
            missed = 0
            for model in self.models[size]:
                missed += escapes(Toy(), model, self.three, suite, extra, weaken)[0]
            self.assertGreater(missed, 0, "%s without %s still finds everything" % (suite, weaken.__name__))

    def test_the_bound_is_real(self):
        """One state more than the bound, and something gets through: the guarantee claims no more than it has."""
        missed = sum(escapes(Toy(), model, self.three, "sweep")[0] for model in self.models[2])
        self.assertGreater(missed, 0)

    def test_phases_count_as_part_of_a_state(self):
        bus, rng = Toy(2), random.Random(9)
        small = list(machines(2)) + rng.sample(self.three, 4000)
        sized = [(machine, len(product(bus, machine)[0])) for machine in small]
        tried = 0
        for machine in rng.sample(list(machines(2)), 256) + rng.sample(self.three, 300):
            model = model_of(bus, machine)
            if model is None or tried == 30:
                continue
            tried += 1
            for suite, extra in (("sweep", 0), ("depth", 1)):
                bound = model.n + extra
                candidates = [m for m, size in sized if size <= bound]
                missed, shown = escapes(bus, model, candidates, suite, extra)
                self.assertEqual(missed, 0, "%s: %d of %d got through" % (suite, missed, shown))
        self.assertEqual(tried, 30)


if __name__ == "__main__":
    unittest.main()
