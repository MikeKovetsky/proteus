# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Equivalence testing: what decides that learning may stop, and what that decision guarantees.

Two complete test suites, both by the Wp-method (Fujiwara and others, 1991), for a device that is deterministic,
returns to one state on reset, and is seen at whole symbols of its bus. n is the number of learned states, with
the bus phase counted as part of a state.

  sweep   Wp with no extra state over every symbol the bus allows, every byte value included. If the device has
          at most n states, the model is exact.
  depth   Wp with m extra states over the symbols learned in full. If the device has at most n + m states and
          treats every other symbol as it treats that kind's default, the model is exact.

Neither can find what lies beyond its bound, and nor can any other black-box method. The other stages are there
to find counterexamples sooner and to catch devices outside the assumptions. They add nothing to the guarantee.

  quick   each learned transition with the short sequence that identifies its target
  scout   each byte from each state, followed by probes of the commands found so far
  streaks the same symbol, and the same transaction, eight times over (counters and lock-outs)
  walks   seeded random walks, each ended by an identifying sequence
  pairs   every opening of two bytes, from every state that waits for a transaction, PAIRS of them to a
          question and each question ended by reading back every command found. The suites take a byte not
          learned in full to act as the default does, and follow a learned command's next byte only with the
          words that tell the model's states apart, so a command with an argument (a bank switch, say) can hide
          from both. It is the costliest stage, so it runs last but one, and stops short where the budget would
          run out
  audit   scouting again in the opposite order from each phase's first state, transitions again in reverse
          order, with pauses, and after a reset of a dirtied device"""

from .bus import Refused

STREAK = 8
PAIRS = 128  # two-byte openings in one question of the pairs stage
SPARE = 10   # the pairs stage stops where less than a tenth of the budget would be left
MULTIPLIERS = (101, 107, 109, 113, 127, 131)  # prime to the size of every bus phase
DEEPEST = 32


class Random:
    def __init__(self, seed):
        self.x = ((seed + 1) * 2654435761) & 0xFFFFFFFF or 1

    def below(self, n):
        x = self.x
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self.x = x
        return x % n


def coprime(a, b):
    while b:
        a, b = b, a % b
    return a == 1


class Search:
    def __init__(self, learner):
        self.learner = learner
        self.bus = learner.bus
        self.told = learner.told
        self.scanned = []       # per state: how far the scout has come in this pass
        self.scout_pass = 0
        self.scout_words = None  # how large the alphabet was when this pass began
        self.sweep_from = (0, 0)
        self.depth_from = 0
        self.pairs_from = (0, 0)
        self.pairs_cover = None  # (questions asked, questions in a full pass) on the last pass
        self.found_by = {}
        self.extra = 0
        self.short = ""  # why the depth suite cannot run, if it cannot
        self.report = {}
        self.tests = 0
        self.checked = 0

    # ---- one test

    def test(self, model, q, word, pauses=(), pause_us=0):
        """None, or the counterexample: the state and the word up to the first wrong answer."""
        word = model.legal(q, word)
        before = len(model.access[q])
        whole = model.access[q] + word
        expected = model.access_out[q] + model.run(q, word)[0]
        shifted = [before + p for p in pauses]
        got = self.learner.ask(whole, shifted, pause_us)
        self.tests += 1
        self.checked += len(whole)
        if got == expected:
            return None
        i = 0
        while got[i] == expected[i]:
            i += 1
        again = self.learner.ask(whole)
        if self.learner.ask(whole) != again:
            raise Refused("nondeterministic", "two answers to the same question")
        if again[:i + 1] != got[:i + 1]:
            if shifted:
                raise Refused("timing", "the device's answers change after a pause of %d us" % pause_us)
            raise Refused("nondeterministic", "an answer changed when the question was repeated")
        if i < before:
            raise Refused("nondeterministic", "the way to a learned state no longer gives the same answers")
        return q, word[:i - before + 1]

    # ---- stages

    def prepare(self, model):
        if not model.minimal():
            raise Refused("internal", "two learned states cannot be told apart")
        for q in range(model.n):
            if model.run(0, model.access[q]) != (model.access_out[q], q):
                raise Refused("internal", "a state's access word does not reach it")
        self.whole, self.identify = model.identifiers()
        self.order = []
        for q in range(model.n):
            self.order.append([a for a in model.alphabet if a in model.rows[q]])

    def quick(self, model):
        for q in range(model.n):
            for word in self.whole[model.phases[q]]:
                found = self.test(model, q, word)
                if found:
                    return found
            for a in self.order[q]:
                for word in self.identify[model.rows[q][a][0]]:
                    found = self.test(model, q, (a,) + word)
                    if found:
                        return found
        return None

    def permuted(self, phase, i):
        count = self.bus.count(phase)
        seed = self.told["seed"]
        step = MULTIPLIERS[seed % len(MULTIPLIERS)]
        if count % step == 0:  # only a list of inputs can be that long
            step = 1
        return self.bus.nth(phase, (seed * 17 + i * step) % count)

    def scouted(self, model, q, i, turn):
        phase = model.phases[q]
        s = self.permuted(phase, i)
        if self.bus.scouts(phase, s):
            t = model.step(q, s)[0]
            for wanted in self.bus.witnesses(model, phase, s, turn):
                found = self.test(model, q, (s,) + model.plan(t, wanted)[0])
                if found:
                    return found
        return None

    def scout(self, model):
        if self.bus.inputs is not None:
            return None
        while True:
            if self.scout_words is None:
                self.scout_words = len(model.alphabet)
            for q in range(model.n):
                if q >= len(self.scanned):
                    self.scanned.append(0)
                phase = model.phases[q]
                while self.scanned[q] < self.bus.count(phase):
                    i = self.scanned[q]
                    found = self.scouted(model, q, i, i + self.scout_pass)
                    if found:
                        return found
                    self.scanned[q] = i + 1
            if self.scout_words == len(model.alphabet):
                return None
            # Probes are made of what has been found, so what was scanned with fewer is scanned again.
            self.scout_words = len(model.alphabet)
            self.scout_pass += 1
            self.scanned = [0] * model.n

    def streaks(self, model):
        shapes = self.bus.transactions(model)
        for q in range(model.n):
            for a in self.order[q]:
                word, end = model.plan(q, (a,) * STREAK)
                if len(word) > 1:
                    found = self.test(model, q, word + self.identify[end][0])
                    if found:
                        return found
            if len(self.order[q]) and model.phases[q] == 0:
                for shape in shapes:
                    word, end = model.plan(q, shape * STREAK)
                    found = self.test(model, q, word + self.identify[end][0])
                    if found:
                        return found
        return None

    def walks(self, model):
        random = Random(self.told["seed"] * 8191 + model.n * 131 + len(model.alphabet))
        for _ in range(self.told["walks"]):
            start = q = random.below(model.n)
            word = []
            for _ in range(3 + random.below(22)):
                phase = model.phases[q]
                if self.order[q] and random.below(5):
                    s = self.order[q][random.below(len(self.order[q]))]
                elif self.bus.count(phase):
                    s = self.bus.nth(phase, random.below(self.bus.count(phase)))
                else:
                    break
                word.append(s)
                q = model.step(q, s)[0]
            ends = self.identify[q]
            found = self.test(model, start, tuple(word) + ends[random.below(len(ends))])
            if found:
                return found
        return None

    def sweep(self, model):
        first, at = self.sweep_from
        for turn in range(model.n + 1):
            q = (first + turn) % model.n
            phase = model.phases[q]
            begin, end = 0, self.bus.count(phase)
            if turn == 0:
                begin = min(at, end)
            elif turn == model.n:
                end = min(at, end)
            if turn < model.n:
                for word in self.whole[phase]:
                    found = self.test(model, q, word)
                    if found:
                        self.sweep_from = (q, begin)
                        return found
            for i in range(begin, end):
                s = self.permuted(phase, i)
                for word in self.identify[model.step(q, s)[0]]:
                    found = self.test(model, q, (s,) + word)
                    if found:
                        self.sweep_from = (q, i)
                        return found
        return None

    def pairs(self, model):
        """None, or a counterexample. Like the sweep, a pass resumes where the last counterexample was found and
        comes round to it again, so the pass that finds nothing has covered the final model."""
        self.pairs_cover = None
        firsts = self.bus.openers(model)
        modes = [q for q in range(model.n) if model.phases[q] == 0]
        if not firsts or not modes:
            return None
        size = len(firsts) * 256
        questions = (size + PAIRS - 1) // PAIRS
        step = 40503  # near 0.618 of 65536, so that one question holds openings of many first bytes
        while not coprime(step, size):
            step += 2
        back = self.bus.readback(model)
        first, at = self.pairs_from
        first %= len(modes)
        floor = self.told["budget"] // SPARE
        asked = 0
        for turn in range(len(modes) + 1):
            p = (first + turn) % len(modes)
            q = modes[p]
            begin, end = 0, questions
            if turn == 0:
                begin = min(at, end)
            elif turn == len(modes):
                end = min(at, end)
            for k in range(begin, end):
                word = []
                for i in range(k * PAIRS, min(size, (k + 1) * PAIRS)):
                    j = (self.told["seed"] * 17 + i * step) % size
                    word += self.bus.opening(model, firsts[j >> 8], j & 0xFF)
                word += back
                if self.told["budget"] - self.learner.symbols - len(model.access[q]) - len(word) - 2 < floor:
                    self.pairs_cover = (asked, questions * len(modes))
                    return None
                found = self.test(model, q, word)
                if found:
                    self.pairs_from = (p, k)
                    return found
                asked += 1
        self.pairs_cover = (asked, questions * len(modes))
        return None

    def depth(self, model):
        """Every word of up to m + 1 learned symbols from every state, depth first."""
        # Only here can the bound be refused: n grows while the cheaper stages still find counterexamples, and
        # the extra states that a declared size asks for shrink with it.
        if self.choose_depth(model) is None:
            raise Refused("bound", self.short)
        m = self.extra
        for turn in range(model.n):
            start = (self.depth_from + turn) % model.n
            word = []
            stack = [(start, -1)]  # the state, and which of its symbols was taken last
            while stack:
                q, taken = stack[-1]
                level = len(stack) - 1
                options = self.order[q] if level <= m else ()
                if taken < 0:
                    tails = self.whole[model.phases[q]] if level <= m else self.identify[q]
                    for tail in tails:
                        if tail or not options:  # an empty tail is a prefix of the next test
                            found = self.test(model, start, tuple(word) + tail)
                            if found:
                                self.depth_from = start
                                return found
                taken += 1
                if taken < len(options):
                    stack[-1] = (q, taken)
                    del word[level:]
                    word.append(options[taken])
                    stack.append((model.rows[q][options[taken]][0], -1))
                else:
                    stack.pop()
                    del word[max(0, level - 1):]
        return None

    def audit(self, model):
        # A device that a reset does not clear shows it when the order of the questions changes.
        if self.bus.inputs is None:
            phases = []
            for q in range(model.n):
                phase = model.phases[q]
                if phase in phases:
                    continue
                phases.append(phase)
                for i in range(self.bus.count(phase) - 1, -1, -1):
                    found = self.scouted(model, q, i, i + self.scout_pass + 1)
                    if found:
                        return found
        quiet, hold = self.told["quiet_us"], self.told["hold_us"]
        for q in range(model.n - 1, -1, -1):
            for a in reversed(self.order[q]):
                word = (a,) + self.identify[model.rows[q][a][0]][-1]
                found = self.test(model, q, word)
                if found:
                    return found
                if quiet:
                    found = self.test(model, q, word, (0, 1), quiet)
                    if found:
                        return found
        first = model.plan(0, self.bus.probe(model, 0))[0]
        for q in range(model.n):
            probe = model.plan(q, self.bus.probe(model, q))[0]
            if hold:
                found = self.test(model, q, probe, (0,), hold)
                if found:
                    return found
            if first:
                self.learner.ask(model.access[q] + probe)  # dirty the device, then reset it
                if self.learner.ask(first) != model.run(0, first)[0]:
                    if self.test(model, 0, first) is None:
                        raise Refused("reset", "reset does not always bring the device back to one state")
                found = self.test(model, 0, first)
                if found:
                    return found
        return None

    # ---- what each suite costs, in bus symbols

    def depth_cost(self, model, most, limit):
        """[the cost with m extra states, for m = 0 and up], as far as `most` or the first over `limit`."""
        counts = [1] * model.n
        lengths = [len(model.access[q]) + 2 for q in range(model.n)]  # 2 is a reset
        running, costs = 0, []
        for level in range(most + 2):
            leaf = inner = 0
            after_counts, after_lengths = [0] * model.n, [0] * model.n
            for q in range(model.n):
                count, length = counts[q], lengths[q]
                if not count:
                    continue
                options = self.order[q]
                for tail in self.identify[q]:
                    leaf += length + count * len(tail)
                for tail in self.whole[model.phases[q]]:
                    if tail or not options:
                        inner += length + count * len(tail)
                for a in options:
                    t = model.rows[q][a][0]
                    after_counts[t] += count
                    after_lengths[t] += length + count
            if level:
                costs.append(running + leaf)
                if running + leaf > limit:
                    break
            running += inner
            counts, lengths = after_counts, after_lengths
        return costs

    def choose_depth(self, model):
        told = self.told
        wanted = told["extra"]
        declared = told["max_states"]
        if declared is not None:
            if model.n > declared:
                raise Refused("bound", "the device has at least %d states, more than the %d declared"
                              % (model.n, declared))
            wanted = declared - model.n
        left = told["budget"] - self.learner.symbols
        if wanted > DEEPEST:
            self.short = ("the bound asked for needs %d extra states, and %d is the most this host will try. "
                          "No program is written" % (wanted, DEEPEST))
            return None
        most = wanted if declared is not None else max(wanted, DEEPEST)
        costs = self.depth_cost(model, most, left)
        if len(costs) <= wanted or costs[wanted] > left:
            affordable = -1
            for m in range(len(costs)):
                if costs[m] <= left:
                    affordable = m
            reach = "%d" % affordable if affordable >= 0 else "none"
            self.short = ("the bound asked for needs %d extra states, and the budget reaches %s. "
                          "No program is written" % (wanted, reach))
            return None
        self.extra = wanted
        while self.extra + 1 < len(costs) and costs[self.extra + 1] <= told["effort"]:
            self.extra += 1
        return costs[self.extra]

    def sweep_cost(self, model):
        total = 0
        for q in range(model.n):
            total += self.bus.count(model.phases[q]) * (len(model.access[q]) + 6)
        return total

    # ---- all of it

    def find(self, model):
        self.prepare(model)
        self.tests = self.checked = 0
        depth_cost = self.choose_depth(model)
        stages = [("quick", self.quick), ("scout", self.scout), ("streaks", self.streaks), ("walks", self.walks)]
        suites = [("sweep", self.sweep), ("depth", self.depth)]
        if depth_cost is not None and depth_cost < self.sweep_cost(model):
            suites.reverse()
        report = {}
        for name, stage in stages + suites + [("pairs", self.pairs), ("audit", self.audit)]:
            tests = self.tests
            found = stage(model)
            report[name] = self.tests - tests
            if found:
                self.found_by[name] = self.found_by.get(name, 0) + 1
                return found
        self.report = report
        return None
