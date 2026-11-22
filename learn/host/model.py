# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The learned machine: a Mealy machine over the symbols learned in full, with a default for every other."""


class Model:
    def __init__(self, bus, phases, rows, alphabet, defaults, access, access_out):
        self.bus = bus
        self.phases = phases          # per state: the bus phase it is reached in
        self.rows = rows              # per state: {symbol: (next state, answer)}
        self.alphabet = alphabet      # the symbols learned in full
        self.known = set(alphabet)
        self.defaults = defaults      # per kind: the symbol every other symbol of that kind is taken to equal
        self.access = access          # per state: a word that reaches it from reset
        self.access_out = access_out  # and the device's answers to that word
        self.n = len(rows)
        self._levels = None

    def step(self, q, s):
        """(next state, answer), or (None, -1) where the bus allows no such symbol."""
        if q is None:
            return None, -1
        found = self.rows[q].get(s)
        if found is None and s not in self.known and self.defaults:
            kind = self.bus.kind(s)
            if kind is not None:
                found = self.rows[q].get(self.defaults[kind])
        return found or (None, -1)

    def run(self, q, word):
        out = []
        for s in word:
            q, o = self.step(q, s)
            out.append(o)
        return tuple(out), q

    def path(self, q, word):
        """The state before each symbol, and the one after the last."""
        states = [q]
        for s in word:
            q = self.step(q, s)[0]
            states.append(q)
        return states

    def plan(self, q, wanted):
        """`wanted` without the symbols the bus would not allow where they fall."""
        word = []
        for s in wanted:
            t = self.step(q, s)[0]
            if t is not None:
                word.append(s)
                q = t
        return tuple(word), q

    def legal(self, q, word):
        for i in range(len(word)):
            q = self.step(q, word[i])[0]
            if q is None:
                return tuple(word[:i])
        return tuple(word)

    # ---- telling states apart

    def levels(self):
        """Partition refinement: levels[r][q] is q's class by words of at most r symbols."""
        if self._levels is None:
            first = {}
            for q in range(self.n):
                if self.phases[q] not in first:
                    first[self.phases[q]] = len(first)
            levels = [[first[self.phases[q]] for q in range(self.n)]]
            count = len(first)
            while True:
                last = levels[-1]
                ids = {}
                now = []
                for q in range(self.n):
                    sig = [last[q]]
                    row = self.rows[q]
                    for a in self.alphabet:
                        if a in row:
                            sig.append(row[a][1])
                            sig.append(last[row[a][0]])
                    key = tuple(sig)
                    if key not in ids:
                        ids[key] = len(ids)
                    now.append(ids[key])
                if len(ids) == count:
                    break
                levels.append(now)
                count = len(ids)
            self._levels = levels
        return self._levels

    def minimal(self):
        seen = set(self.levels()[-1])
        return len(seen) == self.n

    def separated_at(self, p, q):
        levels = self.levels()
        for r in range(len(levels)):
            if levels[r][p] != levels[r][q]:
                return r
        return -1

    def separator(self, p, q):
        """A shortest word that tells p from q, two states of one phase."""
        levels = self.levels()
        r = self.separated_at(p, q)
        word = []
        while r > 0:
            chosen = None
            for a in self.alphabet:
                if a in self.rows[p]:
                    if self.rows[p][a][1] != self.rows[q][a][1]:
                        chosen = a
                        r = 1
                        break
                    if chosen is None and levels[r - 1][self.rows[p][a][0]] != levels[r - 1][self.rows[q][a][0]]:
                        chosen = a
            word.append(chosen)
            p, q = self.rows[p][chosen][0], self.rows[q][chosen][0]
            r = 0 if r == 1 else self.separated_at(p, q)
        return tuple(word)

    def identifiers(self):
        """(W per phase, W_i per state) for the Wp-method. W_i tells state i from every other of its phase, and
        W is the union of a phase's W_i, so it tells every pair of that phase apart."""
        identify, whole = [], {}
        for q in range(self.n):
            others = [p for p in range(self.n) if p != q and self.phases[p] == self.phases[q]]
            others.sort(key=lambda p: (-self.separated_at(q, p), p))
            mine = []
            while others:
                word = self.separator(q, others[0])
                answer = self.run(q, word)[0]
                others = [p for p in others if self.run(p, word)[0] == answer]
                mine.append(word)
            identify.append(mine or [()])
            union = whole.setdefault(self.phases[q], [])
            for word in mine:
                if word not in union:
                    union.append(word)
        for phase in whole:
            if not whole[phase]:
                whole[phase].append(())
        return whole, identify
