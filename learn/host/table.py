# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Table generation: a graph of entries, with equal ones shared, packed into the chip's 512 places.

The chip's next address is base | cA | cB << 1 (docs/isa.md, 2.1). A branch on one condition therefore has five
places for its two successors in a block of four, not one: the condition may be ORed into bit 0 (cA), into
bit 1 (cB) or into both (cA and cB select the same flag):

    (0,1)  (2,3)    cA
    (0,2)  (1,3)    cB
    (0,3)           cA and cB

So branches share successors. Up to three branches with one default can share its entry in a block of four, and
so can three with one target. The compiler puts every two-way branch in an aligned pair. This does not.

A field's encoding is docs/isa.md, section 2. test/learn checks it against the repository's own entry.pack."""

DEPTH = 512
SEARCH = 4096  # placement may run past the table, to say how large a machine that does not fit is
MAX_TCAM = 16

NEVER, A, B, C, EDGE, TZ, KZ, JZ, NZ, D, CRC, RX0, RX3, TX1, TX2, EQUAL = range(16)
D_IN = RX3  # v2: cond 12 selects 4th input D when in_d_en=1
LOW, HIGH, RELEASE, DATA, NDATA, TOGGLE, OPEN_DRAIN = range(1, 8)
TO_S, TO_R, TO_N, TO_NHI, PUSH1, PUSH2, LINK = range(1, 8)
DIN_0, DIN_1, DIN_A, DIN_B = range(4)
RETURN = 3
J_LOAD, K_DEC, K_LOAD = 2, 3, 6
GATE_IF, GATE_UNLESS, GATE_DISPATCH = 1, 2, 3
DISP_SLICE2, DISP_NIBBLE, DISP_TCAM8, DISP_TCAM16 = range(4)
PLACES = ((0, 3), (0, 1), (0, 2), (1, 3), (2, 3))  # the five, the one that others can share most easily first


class Node:
    def __init__(self, word_a, word_b, gated):
        self.word_a = word_a
        self.word_b = word_b
        self.gated = gated  # its condition gates the pins, so it has to stay cA
        self.dispatch = None  # v2: (mode, cb) when gate == GATE_DISPATCH
        self.conds = ()
        self.succ = ()
        self.back = None    # for a call: the node to return to


class Table:
    def __init__(self):
        self.nodes = []

    def node(self, pin=None, gate=0, shift=0, din=0, imm=0, src=0, dst=0, nop=0, kj=0, tload=0):
        """One entry. The first node made is where the machine starts. `pin` is (logical output, operation)."""
        a = gate << 27 | din << 30
        if pin is not None:
            a |= pin[0] << 17 | pin[1] << 19
        b = imm | src << 16 | dst << 19 | shift << 22 | nop << 26 | kj << 28 | tload << 31
        self.nodes.append(Node(a, b, gate != 0))
        return len(self.nodes) - 1

    def go(self, node, *succ):
        self.nodes[node].succ = succ
        self.nodes[node].conds = ()
        return node

    def when(self, node, conds, succ):
        """The successors in the order neither, cA, cB, both. None is a place that is never reached."""
        self.nodes[node].conds = tuple(conds)
        self.nodes[node].succ = tuple(succ)
        return node

    def dispatch(self, node, mode, cb, succ):
        """v2: 1-clock indexed / TCAM dispatch (gate == 3, pin ops off). len(succ) is 4, 8 or 16."""
        n = self.nodes[node]
        n.word_a = (n.word_a & ~(3 << 27)) | (GATE_DISPATCH << 27)
        n.gated = False
        n.dispatch = (mode, cb)
        n.conds = ()
        n.succ = tuple(succ)
        return node

    def call(self, target, back):
        node = self.go(self.node(dst=LINK), target)
        self.nodes[node].back = back
        return node

    # ---- sharing

    def simplify(self, node):
        if node.dispatch is not None:
            return
        conds, succ = node.conds, node.succ
        if len(conds) == 2:
            if succ[0] == succ[2] and succ[1] == succ[3]:
                conds, succ = conds[:1], succ[:2]
            elif not node.gated and succ[0] == succ[1] and succ[2] == succ[3]:
                conds, succ = conds[1:], (succ[0], succ[2])
        if len(conds) == 1 and not node.gated and succ[0] == succ[1]:
            conds, succ = (), succ[:1]
        node.conds, node.succ = conds, succ

    def share(self):
        """Merge the nodes that nothing can tell apart (partition refinement), and drop what is never reached."""
        while True:
            for node in self.nodes:
                self.simplify(node)
            reached = {0: True}
            order = [0]
            for at in order:  # it grows as it goes
                node = self.nodes[at]
                for to in node.succ + (node.back,):
                    if to is not None and to not in reached:
                        reached[to] = True
                        order.append(to)
            group, ids = {}, {}
            for at in order:
                node = self.nodes[at]
                key = (node.word_a, node.word_b, node.conds, node.dispatch, len(node.succ), node.back is None)
                if key not in ids:
                    ids[key] = len(ids)
                group[at] = ids[key]
            count = len(ids)
            while True:
                now, ids = {}, {}
                for at in order:
                    node = self.nodes[at]
                    key = [group[at], -1 if node.back is None else group[node.back]]
                    for to in node.succ:
                        key.append(-1 if to is None else group[to])
                    key = tuple(key)
                    if key not in ids:
                        ids[key] = len(ids)
                    now[at] = ids[key]
                group = now
                if len(ids) == count:
                    break
                count = len(ids)
            fresh, done = [], set()
            for at in order:  # groups are numbered in this order, so the start is still node 0
                if group[at] in done:
                    continue
                done.add(group[at])
                old = self.nodes[at]
                node = Node(old.word_a, old.word_b, old.gated)
                node.dispatch = old.dispatch
                node.conds = old.conds
                node.succ = tuple([None if to is None else group[to] for to in old.succ])
                node.back = None if old.back is None else group[old.back]
                fresh.append(node)
            same = len(fresh) == len(self.nodes)
            self.nodes = fresh
            if same:
                return

    # ---- packing

    def pack(self):
        self.share()
        disps, fours, twos, ones = [], [], [], []
        known = set()
        use = [0] * len(self.nodes)
        for node in self.nodes:
            is_disp = node.dispatch is not None
            key = (node.succ, node.gated, is_disp)
            if node.succ and key not in known:
                known.add(key)
                if is_disp:
                    disps.append(key)
                elif len(node.succ) == 1:
                    ones.append(key)
                elif len(node.succ) == 2:
                    twos.append(key)
                else:
                    fours.append(key)
                for to in node.succ:
                    if to is not None:
                        use[to] += 1
            if node.back is not None:
                key = ((node.back,), False, False)
                if key not in known:
                    known.add(key)
                    ones.append(key)
        disps.sort(key=lambda k: -len(k[0]))
        hubs = sorted(range(len(twos)), key=lambda i: (-use[twos[i][0][0]] - use[twos[i][0][1]], i))
        orders = (list(range(len(twos))), hubs, list(range(len(twos) - 1, -1, -1)))
        best = None
        for order in orders:
            packed = self.place(disps, fours, [twos[i] for i in order], ones)
            if best is None or packed.top < best.top:
                best = packed
        return best

    def place(self, disps, fours, twos, ones):
        return Placement(self.nodes).fill(disps, fours, twos, ones)


class Placement:
    def __init__(self, nodes):
        self.nodes = nodes
        self.at = {}     # address: node
        self.where = {}  # node: its addresses
        self.where_to = {}  # a successor block: (base, the low bits' use)
        self.top = 0     # one past the highest address in use
        self.put(0, 0)

    def put(self, addr, node):
        if node is None or addr in self.at:
            return
        self.at[addr] = node
        self.where.setdefault(node, []).append(addr)
        if addr >= self.top:
            self.top = addr + 1

    def cost(self, addr, node):
        if node is None:
            return 0
        here = self.at.get(addr)
        if here is None:
            return 1
        return 0 if here == node else 9

    def hole(self):
        addr = 0
        while addr in self.at:
            addr += 1
        return addr

    def fill(self, disps, fours, twos, ones):
        for key in disps:
            succ, _gated, _is_disp = key
            size = len(succ)
            best = (999, 0)
            for base in range(0, SEARCH, size):
                total = 0
                ok = True
                for k in range(size):
                    c = self.cost(base + k, succ[k])
                    if c >= 9:
                        ok = False
                        break
                    total += c
                if ok and total < best[0]:
                    best = (total, base)
                    if total == 0 or base >= self.top:
                        break
            base = best[1]
            for k in range(size):
                self.put(base + k, succ[k])
            self.where_to[key] = (base, 0)

        for key in fours:
            succ, gated, _is_disp = key
            best = (99, 0, False)
            orders = ((False, succ),) if gated else ((False, succ), (True, (succ[0], succ[2], succ[1], succ[3])))
            for base in range(0, SEARCH, 4):
                for swap, want in orders:
                    total = self.cost(base, want[0]) + self.cost(base + 1, want[1])
                    total += self.cost(base + 2, want[2]) + self.cost(base + 3, want[3])
                    if total < best[0]:
                        best = (total, base, swap)
                if best[0] == 0 or (base >= self.top and best[0] < 9):
                    break
            total, base, swap = best
            want = (succ[0], succ[2], succ[1], succ[3]) if swap else succ
            for k in range(4):
                self.put(base + k, want[k])
            self.where_to[key] = (base, swap)

        left = list(twos)
        while left:
            choice = None
            index = 0
            while index < len(left):
                found = self.near(left[index])
                if found and found[0] == 0:
                    self.settle(left.pop(index), found)
                    continue
                if found and choice is None:
                    choice = (index, found)
                index += 1
            if choice is None and left:
                choice = (0, self.anywhere(left[0]))
            if choice is not None:
                self.settle(left.pop(choice[0]), choice[1])

        for key in ones:
            node = key[0][0]
            if node not in self.where:
                self.put(self.hole(), node)
            self.where_to[key] = (self.where[node][0], 0)
        return self

    def settle(self, key, found):
        _, low, high = found
        self.put(low, key[0][0])
        self.put(high, key[0][1])
        self.where_to[key] = (low, high ^ low)

    def near(self, key):
        """The cheapest place for a pair beside a copy of either half: at most one new entry."""
        (first, second), gated, _is_disp = key
        best = None
        for side in (0, 1):
            have, other = (first, second) if side == 0 else (second, first)
            for addr in self.where.get(have, ()):
                block = addr & ~3
                for place in PLACES:
                    if block + place[side] != addr or (gated and place[0] ^ place[1] != 1):
                        continue
                    extra = self.cost(block + place[1 - side], other)
                    if extra < 2 and (best is None or extra < best[0]):
                        best = (extra, block + place[0], block + place[1])
                        if extra == 0:
                            return best
        return best

    def anywhere(self, key):
        (first, second), gated, _is_disp = key
        part = None
        for block in range(0, SEARCH, 4):
            used = 0
            for k in range(4):
                if block + k in self.at:
                    used += 1
            if used > 2:
                continue
            for low, high in PLACES:
                if gated and low ^ high != 1:
                    continue
                if self.cost(block + low, first) + self.cost(block + high, second) <= 2:
                    return (2, block + low, block + high)
            if block >= self.top:
                break
        return (2, block, block + 1)

    def words(self):
        """(word A, word B) for every address up to the top. A place nobody uses is all zeros."""
        out = []
        for addr in range(self.top):
            index = self.at.get(addr)
            if index is None:
                out.append((0, 0))
                continue
            node = self.nodes[index]
            a, b = node.word_a, node.word_b
            first = second = 0
            if node.dispatch is not None:
                base, _ = self.where_to[(node.succ, node.gated, True)]
                a |= base
                first, second = node.dispatch
            elif node.succ:
                base, how = self.where_to[(node.succ, node.gated, False)]
                a |= base
                if len(node.succ) == 4:
                    first, second = node.conds
                    if how:
                        first, second = second, first
                elif len(node.succ) == 2:
                    first = node.conds[0] if how & 1 else 0
                    second = node.conds[0] if how & 2 else 0
            elif node.conds:
                first = node.conds[0]  # a return's gate
            if node.back is not None:
                b |= self.where[node.back][0]
            out.append((a | first << 9 | second << 13, b))
        return out

    def image(self, outs, ins, msb=1, in_d=None, tcam=None):
        in_d_en = 1 if in_d is not None else 0
        in_d_pin = (in_d & 7) if in_d is not None else 0
        head = [
            outs[0] | outs[1] << 3,
            outs[2] | outs[3] << 3,
            ins[0] | ins[1] << 3 | in_d_en << 6,
            ins[2] | msb << 3 | in_d_pin << 4,
        ]
        if self.top > DEPTH:
            raise ValueError("%d entries do not fit in %d" % (self.top, DEPTH))
        has_tcam = bool(tcam)
        data = bytearray([1 | (0x80 if has_tcam else 0)] + head + [0] * 12)
        if has_tcam:
            data.append(len(tcam))
            for val, mask, cls in tcam:
                data.append(val & 0xFF)
                data.append(mask & 0xFF)
                data.append(cls & 0x0F)
        words = self.words()
        data.append(len(words) & 0xFF)
        data.append(len(words) >> 8)
        for pair in words:
            for word in pair:
                for shift in (0, 8, 16, 24):
                    data.append((word >> shift) & 0xFF)
        return bytes(data)


class Chains:
    """The compare chain of a state: one entry for each byte that does not do what its default does. Every
    chain is in one order, the commonest (byte, target) last, so chains that end alike share their ends."""

    def __init__(self, table):
        self.table = table
        self.wanted = []
        self.often = {}
        self.longest = 0

    def want(self, cases, default, then):
        """cases is [(byte, target)]. then(node) is called with the chain's first node once all are known."""
        cases = [case for case in cases if case[1] != default]
        for case in cases:
            self.often[case] = self.often.get(case, 0) + 1
        self.wanted.append((cases, default, then))
        if len(cases) > self.longest:
            self.longest = len(cases)

    def build(self):
        for cases, default, then in self.wanted:
            node = default
            for byte, target in sorted(cases, key=lambda case: (-self.often[case], case)):
                test = self.table.node(imm=byte)
                self.table.when(test, (EQUAL,), (node, target))
                node = test
            then(node)
