# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The learner: a classification tree (Kearns and Vazirani's form of Angluin's L*), with counterexamples cut
down by Rivest and Schapire's binary search. It sees a bus adapter and a link to the chip, and nothing else.

Only the symbols in the alphabet are learned per state. Any other symbol is taken to behave as its kind's
default until a test says otherwise, and then it joins the alphabet."""

from .bus import Refused
from .model import Model


class Node:
    def __init__(self, parent=None, key=None, state=None):
        self.parent = parent
        self.key = key      # the answer that leads here from the parent
        self.state = state  # a leaf's state. None on an inner node, which has a suffix
        self.suffix = None
        self.kids = None


class Learner:
    def __init__(self, bus, link, told):
        self.bus = bus
        self.link = link
        self.told = told
        self.roots = {}
        self.access = []
        self.access_out = []
        self.phases = []
        self.leaves = []
        self.cells = []  # per state: {symbol: [the node it leads to, the answer]}
        self.alphabet, self.defaults = bus.alphabet()
        self.learned = set(self.alphabet)
        self.symbols = 0
        self.queries = 0
        self.counterexamples = 0

    # ---- questions

    def ask(self, word, pauses=(), pause_us=0):
        if self.symbols + len(word) + 2 > self.told["budget"]:
            raise Refused("budget", "the budget of %d bus symbols ran out" % self.told["budget"])
        self.symbols += len(word) + 2
        self.queries += 1
        raw, errors = self.link.exchange(self.bus.encode(word, pauses, pause_us), len(word))
        if raw is None or len(raw) != len(word):
            raise Refused("link", "the chip did not answer")
        if errors:
            raise Refused("stuck", "the query engine found the bus stuck: the clock was held low for 1 ms")
        return self.bus.decode(word, raw)

    def query(self, prefix, prefix_out, suffix):
        got = self.ask(prefix + suffix)
        if got[:len(prefix)] != prefix_out:
            raise Refused("nondeterministic", "the device answered a question differently from the last time: "
                          "it is not deterministic from reset, or the wiring is noisy")
        return got[len(prefix):]

    def from_state(self, q, suffix):
        return self.query(self.access[q], self.access_out[q], suffix)

    # ---- the tree

    def add_state(self, node, word, out, phase):
        if len(self.access) >= self.told["limit_states"]:
            raise Refused("states", "more than %d states: the device stores data, or is too large to copy"
                          % self.told["limit_states"])
        node.state = len(self.access)
        self.access.append(word)
        self.access_out.append(out)
        self.phases.append(phase)
        self.leaves.append(node)
        self.cells.append({})
        return node

    def sift(self, word, out, phase, node=None):
        if node is None:
            node = self.roots.get(phase)
            if node is None:
                node = self.roots[phase] = Node()
                return self.add_state(node, word, out, phase)
        while node.state is None:
            answer = self.query(word, out, node.suffix)
            child = node.kids.get(answer)
            if child is None:
                if self.query(word, out, node.suffix) != answer:
                    raise Refused("nondeterministic", "two answers to the same question")
                child = node.kids[answer] = Node(node, answer)
                return self.add_state(child, word, out, phase)
            node = child
        return node

    def close(self):
        q = 0
        while q < len(self.access):
            word, out, phase = self.access[q], self.access_out[q], self.phases[q]
            row, answers = self.cells[q], {}
            for a in self.alphabet:
                if not self.bus.allows(phase, a):
                    continue
                cell = row.get(a)
                if cell is None:
                    o = self.query(word, out, (a,))[0]
                    node = self.sift(word + (a,), out + (o,), self.bus.after(phase, a, o))
                    cell = row[a] = [node, o]
                elif cell[0].state is None:  # its leaf was split since
                    after = self.bus.after(phase, a, cell[1])
                    cell[0] = self.sift(word + (a,), out + (cell[1],), after, cell[0])
                answers[a] = cell[1]
            why = self.bus.check(phase, answers)
            if why:
                raise Refused("unsupported", why)
            q += 1
        rows = []
        for cells in self.cells:
            row = {}
            for a in cells:
                row[a] = (cells[a][0].state, cells[a][1])
            rows.append(row)
        return Model(self.bus, list(self.phases), rows, list(self.alphabet), list(self.defaults), list(self.access),
                     list(self.access_out))

    def unstable(self, model):
        """A place where the model contradicts an answer that shaped the tree: a counterexample for free."""
        for q in range(model.n):
            node = self.leaves[q]
            while node.parent is not None:
                said = model.run(q, node.parent.suffix)[0]
                if said != node.key:
                    for i in range(len(said)):
                        if said[i] != node.key[i]:
                            return q, node.parent.suffix[:i + 1]
                node = node.parent
        return None

    # ---- counterexamples

    def refine(self, model, q, word):
        """The model is wrong about the last answer of `word` from q. Adds a state or a symbol, or refuses."""
        self.counterexamples += 1
        states = model.path(q, word)
        expected = model.run(q, word)[0][-1]

        def agrees(k):
            return self.from_state(states[k], word[k:])[-1] == expected

        if agrees(0):
            raise Refused("nondeterministic", "a counterexample did not repeat")
        last = len(word) - 1
        if last == 0 or not agrees(last):
            low = last  # the last transition's own answer is wrong
        else:
            low, high = 0, last  # wrong from low, right from high
            while high - low > 1:
                middle = (low + high) // 2
                if agrees(middle):
                    high = middle
                else:
                    low = middle
        a, tail = word[low], word[low + 1:]
        if a not in self.learned:
            self.add_symbol(a, states[low], states[low + 1], tail)
            return
        if not tail:
            raise Refused("nondeterministic", "a learned answer changed")
        self.split(states[low], a, states[low + 1], tail)

    def add_symbol(self, a, q, target, tail):
        if len(self.alphabet) >= self.told["limit_alphabet"]:
            raise Refused("alphabet", "more than %d inputs matter: the device stores or computes on data"
                          % self.told["limit_alphabet"])
        kind = self.bus.kind(a)
        if kind is None or not self.defaults:
            raise Refused("nondeterministic", "a learned answer changed")
        # A default that turns out to be the special one would make every other symbol an exception. Three
        # others vote: if they side with the newcomer, it becomes the default.
        size = self.bus.kind_size(kind)
        others, i = [], 1
        while len(others) < 3 and i <= size:
            other = self.bus.member(kind, self.bus.seed * 59 + a * 31 + i * 83)
            if other != a and other not in self.learned and other not in others:
                others.append(other)
            i += 1
        new = self.from_state(q, (a,) + tail)
        old = self.from_state(q, (self.defaults[kind],) + tail)
        if new[-1] == old[-1]:
            # The newcomer does what the default does, and the model is wrong about both: a state is missing.
            if not tail:
                raise Refused("nondeterministic", "a learned answer changed")
            self.split(q, self.defaults[kind], target, tail)
            return
        self.alphabet.append(a)
        self.learned.add(a)
        votes = 0
        for other in others:
            if self.from_state(q, (other,) + tail) == new:
                votes += 1
        if votes >= 2:
            self.defaults[kind] = a

    def split(self, q, a, target, tail):
        out = self.cells[q][a][1]
        word, word_out = self.access[q] + (a,), self.access_out[q] + (out,)
        old = self.from_state(target, tail)
        new = self.query(word, word_out, tail)
        if new == old or self.query(word, word_out, tail) != new:
            raise Refused("nondeterministic", "a counterexample led nowhere")
        inner = self.leaves[target]
        inner.state, inner.suffix = None, tail
        kept = Node(inner, old, target)
        self.leaves[target] = kept
        fresh = Node(inner, new)
        inner.kids = {old: kept, new: fresh}
        self.add_state(fresh, word, word_out, self.bus.after(self.phases[q], a, out))

    # ---- the loop

    def learn(self, search):
        self.add_state(self.roots.setdefault(0, Node()), (), (), 0)
        most = 4 * (self.told["limit_states"] + self.told["limit_alphabet"])
        while True:
            model = self.close()
            found = self.unstable(model) or search.find(model)
            if found is None:
                return model
            if self.counterexamples >= most:
                raise Refused("diverged", "learning did not settle")
            self.refine(model, found[0], found[1])
