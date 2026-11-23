# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The table of a learned SPI target, mode 0. The host writes it from the model, and from nothing else.

    out 0 MISO=P2    in A SCK=P0, B MOSI=P1, C CS=P3, D nRESET=P4

A state is entered at an entry that puts the first bit of its answer on MISO, if SCK is low already, and calls
one subroutine that every state shares. The subroutine puts the first bit out once SCK is low if the entry could
not, takes seven bits in, puts the other seven out and waits for the eighth rising edge. It returns with J = 1, or
with J = 0 when CS rose first. The state it returns to takes the last bit and, on J = 1, decides on the whole
byte: with a chain of compares, one for each byte value that does not do what most do, where that chain is short
enough for the rate; else with RTL v2's 1-clock masked TCAM dispatch when the states that need it share at most
15 non-default classes covered by at most 16 masked rules; or else with a tree of tests on the byte's bits, most
significant first, where each test reads one bit, catches one byte value whole and shifts the next bit up. Chains
share their ends and take fewer entries. TCAM decides in 1 clock (serving up to 6 MHz). A tree decides in at most
eight tests, however many byte values matter.

The time from a byte's last rising edge to the next answer's first bit is AFTER clocks and one a test, and the
first bit needs SCK low for LOW clocks. `period` turns that into the fastest rate, with a clock to spare.

S is cleared where the machine waits for CS, so a waiting machine's registers hang on its state and little
else. The judge in learn/lab can then visit every one of them.

The fourth input D (RTL v2) watches nRESET on P4 while waiting for CS (and when halted), returning to state 0."""

from .bus import Refused
from .spi import DESELECT, HIGH, SELECT
from . import table as T

OUTS = (2, 0, 0, 0)
INS = (0, 1, 3)
IN_D = 4
MISO = 0
SCK, CS, NRESET = T.A, T.C, T.D_IN
J1 = 0x1000
K7 = 0x700
CLOCK_KHZ = 60000
AFTER = 6   # clocks from a byte's last rising edge to its answer's first bit on the pad, with no test on the way
LOW = 4     # clocks SCK must be low before the first bit is on the pad: the synchroniser's 2, an entry, the pad's 1
MARGIN = 1  # a clock for a controller whose clock is not the chip's


def period(depth):
    """The shortest SCK period, in the chip's clocks, at which a decision of `depth` tests is in time: the first
    bit of the next answer is on the pad within a period of the last rising edge, and SCK is low, half the period,
    long enough for it to go out. Each bound keeps MARGIN to spare."""
    return max(AFTER + depth + MARGIN, 2 * (LOW + MARGIN))


def most_khz(depth):
    return CLOCK_KHZ // period(depth)


class Tree:
    """The decision on a byte: a binary tree on its bits, most significant first, where every test also catches
    one byte value whole. f maps each byte to the node it leads to."""

    def __init__(self, table):
        self.t = table
        self.made = {}

    def node(self, f, values, level):
        """(the node that decides among `values`, which share their first `level` bits, and its depth)."""
        targets = {}
        for x in values:
            targets.setdefault(f[x], []).append(x)
        if len(targets) <= 1:
            return (next(iter(targets)) if targets else None), 0
        key = (level, tuple((x, f[x]) for x in values))
        if key in self.made:
            return self.made[key]
        bit = 0x80 >> level
        halves = ([x for x in values if not x & bit], [x for x in values if x & bit])
        catch = self.catch(f, halves)
        sides = [[x for x in half if x != catch] for half in halves]
        low, low_depth = self.node(f, sides[0], level + 1)
        high, high_depth = self.node(f, sides[1], level + 1)
        test = self.t.node(imm=(catch << level) & 0xFF, shift=1, din=T.DIN_0)
        caught = f[catch]
        # S holds the byte shifted up `level` places, so S[7] is this bit and S == imm is the caught byte
        self.t.when(test, (T.D, T.EQUAL), (low, high, None if catch & bit else caught,
                                           caught if catch & bit else None))
        found = (test, 1 + max(low_depth, high_depth))
        self.made[key] = found
        return found

    @staticmethod
    def catch(f, halves):
        """The byte to catch whole: in the half with more bytes unlike the rest of it, the one whose target is
        rarest there."""
        best = None
        for half in halves:
            counts = {}
            for x in half:
                counts[f[x]] = counts.get(f[x], 0) + 1
            if not counts:
                continue
            odd = len(half) - max(counts.values())
            choice = min(half, key=lambda x: (counts[f[x]], x))
            if best is None or odd > best[0]:
                best = (odd, choice)
        return best[1]


def sends(model, q):
    """The byte a state answers during the byte coming in, the same for every byte (the learner checks that)."""
    row = model.rows[q]
    for s in model.alphabet:
        if s < 256 and s in row:
            return row[s][1]
    return 0


# Precomputed mask lookup for 8-bit TCAM cube compression (256 masks, sorted by care-bit count ascending).
_MASKS_BY_WEIGHT = tuple(sorted(range(256), key=lambda m: (bin(m).count("1"), m)))
_CUBE_CACHE = {}


def _cube_bits(val, mask):
    key = (val, mask)
    bits = _CUBE_CACHE.get(key)
    if bits is None:
        bits = 0
        for x in range(256):
            if (x & mask) == val:
                bits |= 1 << x
        _CUBE_CACHE[key] = bits
    return bits


def compile_tcam(states):
    """Assign each byte 0..255 a TCAM class 0..15 across `states` = [(q, f, left), ...] and cover classes 1..C
    with at most T.MAX_TCAM priority-ordered masked rules (val, mask, cls). Returns (rules, byte_to_cls, n_cls)
    or None if >15 non-default classes or >T.MAX_TCAM rules."""
    sig_bytes = {}
    for x in range(256):
        sig = tuple(f[x] for _, f, _ in states)
        sig_bytes.setdefault(sig, []).append(x)
    if len(sig_bytes) <= 1 or len(sig_bytes) - 1 > 15:
        return None
    # Most common signature is class 0 (TCAM miss default); order remaining classes smallest-first so narrow
    # exceptions take priority and become don't-cares for broader masked rules.
    ordered = sorted(sig_bytes.items(), key=lambda kv: (-len(kv[1]), kv[1][0]))
    default_sig = ordered[0][0]
    others = sorted(ordered[1:], key=lambda kv: (len(kv[1]), kv[1][0]))
    cls_of_sig = {default_sig: 0}
    byte_to_cls = [0] * 256
    class_bits = {}
    for idx, (sig, xs) in enumerate(others, 1):
        cls_of_sig[sig] = idx
        bits = 0
        for x in xs:
            byte_to_cls[x] = idx
            bits |= 1 << x
        class_bits[idx] = (xs, bits)

    rules = []
    dont_care = 0  # bytes already matched by higher-priority rules
    for cls in range(1, len(others) + 1):
        xs, need = class_bits[cls]
        if len(xs) == 1:
            rules.append((xs[0], 0xFF, cls))
            dont_care |= need
            if len(rules) > T.MAX_TCAM:
                return None
            continue
        allowed = need | dont_care
        uncovered = need
        while uncovered:
            best = None  # (covered_count, -care_bits, -val, val, mask, bits)
            rem_xs = [x for x in xs if (uncovered >> x) & 1]
            seen_cubes = set()
            for x in rem_xs:
                for mask in _MASKS_BY_WEIGHT:
                    val = x & mask
                    ckey = (val, mask)
                    if ckey in seen_cubes:
                        continue
                    seen_cubes.add(ckey)
                    cbits = _cube_bits(val, mask)
                    if cbits & ~allowed:
                        continue
                    cov = bin(cbits & uncovered).count("1")
                    cand = (cov, -bin(mask).count("1"), -val, val, mask, cbits)
                    if best is None or cand > best:
                        best = cand
                    if (cbits & uncovered) == uncovered:
                        break  # single cube covers everything left for this class
                if best is not None and (best[5] & uncovered) == uncovered:
                    break
            _, _, _, val, mask, cbits = best
            rules.append((val, mask, cls))
            if len(rules) > T.MAX_TCAM:
                return None
            uncovered &= ~cbits
            dont_care |= cbits

    # Exact verification over all 256 bytes.
    for x in range(256):
        got = 0
        for val, mask, cls in rules:
            if (x & mask) == (val & mask):
                got = cls
                break
        if got != byte_to_cls[x]:
            return None
    return rules, byte_to_cls, len(others)


def _build(bus, model, use_tcam=True):
    t = T.Table()
    gap, entry, root, pre = {}, {}, {}, {}
    for q in range(model.n):  # state 0 waits for CS, so the table starts with it
        if model.phases[q] == HIGH:
            gap[q] = t.node(dst=T.LINK)
    boot = gap[0]
    wait_cs = t.node(pin=(MISO, T.RELEASE), dst=T.TO_S, kj=T.J_LOAD, imm=J1)
    wait_ret = t.node(nop=T.RETURN)
    t.when(wait_cs, (NRESET, CS), (boot, wait_ret, boot, wait_cs))
    trap = t.node(pin=(MISO, T.RELEASE), dst=T.PUSH2, imm=0xEE)  # an input the host was told not to learn
    halt = t.node()
    t.go(trap, halt)
    t.when(halt, (NRESET,), (boot, halt))

    # the subroutine, entered with the answer in S and K = 7, and its first bit on MISO if SCK was low already
    first = t.node(pin=(MISO, T.DATA), gate=T.GATE_UNLESS)
    rise = t.node(pin=(MISO, T.RELEASE), gate=T.GATE_IF)
    take = t.node(shift=1, din=T.DIN_B, kj=T.K_DEC)
    edge = t.node(pin=(MISO, T.RELEASE), gate=T.GATE_IF)
    put = t.node(pin=(MISO, T.DATA), gate=T.GATE_UNLESS)
    last = t.node(pin=(MISO, T.RELEASE), gate=T.GATE_IF)
    done = t.node(nop=T.RETURN)
    out = t.node(pin=(MISO, T.RELEASE), kj=T.J_LOAD)
    t.when(out, (NRESET,), (boot, done))
    t.when(first, (SCK, CS), (rise, first, out, out))  # the first bit, once SCK is low
    t.when(rise, (CS, SCK), (rise, out, take, out))    # SCK rose: take the bit
    t.go(take, edge)
    t.when(edge, (CS, SCK), (put, out, edge, out))     # SCK is low: the next bit may go out
    t.when(put, (CS, T.KZ), (rise, out, last, out))    # seven bits are in
    t.when(last, (CS, SCK), (last, out, done, out))    # the eighth rising edge

    for q in range(model.n):
        if model.phases[q] == HIGH:
            continue
        byte = sends(model, q)
        if byte not in pre:
            pre[byte] = t.go(t.node(dst=T.TO_S, kj=T.K_LOAD, imm=K7 | byte), first)
        root[q] = t.node(shift=1, din=T.DIN_B)  # the eighth bit
        # The first bit goes out here if SCK is low already, and the call happens either way. Its condition only
        # gates the pin, so both of its places lead on.
        entry[q] = t.node(pin=(MISO, T.HIGH if byte & 0x80 else T.LOW), gate=T.GATE_UNLESS, dst=T.LINK,
                          kj=T.J_LOAD, imm=J1)
        t.when(entry[q], (SCK,), (pre[byte], pre[byte]))
        t.nodes[entry[q]].back = root[q]

    # A state decides on its byte with a chain of compares where the chain is short enough for the rate, as
    # chains share their ends and take fewer entries; otherwise with 1-clock TCAM dispatch when the slow states
    # fit in 16 masked rules; otherwise with a decision tree on the byte's bits.
    allowed = 0
    while period(allowed + 1) <= CLOCK_KHZ // bus.serve:
        allowed += 1
    tree, chains = Tree(t), T.Chains(t)
    deepest = 0
    need_tcam = []
    for q in range(model.n):
        row = model.rows[q]
        if q in gap:
            to = row.get(SELECT)
            t.go(gap[q], wait_cs)
            t.nodes[gap[q]].back = entry[to[0]] if to else trap
            continue
        to = row.get(DESELECT)
        left = gap[to[0]] if to else trap
        f = []
        for x in range(256):
            to = model.step(q, x)[0]
            f.append(trap if to is None else entry[to])
        common = max(set(f), key=lambda target: (f.count(target), -target))
        cases = [(x, f[x]) for x in range(256) if f[x] != common]
        if len(cases) <= allowed:
            deepest = max(deepest, len(cases))

            def then(head, node=root[q], left=left):
                t.when(node, (T.JZ,), (head, left))

            chains.want(cases, common, then)
            continue
        need_tcam.append((q, f, left))

    rules = None
    if need_tcam:
        compiled = compile_tcam(need_tcam) if use_tcam else None
        if compiled is not None:
            rules, byte_to_cls, n_cls = compiled
            if n_cls <= 3:
                mode, cb, size = T.DISP_TCAM16, 1, 4
            elif n_cls <= 7:
                mode, cb, size = T.DISP_TCAM8, 0, 8
            else:
                mode, cb, size = T.DISP_TCAM16, 0, 16
            # Representative byte for each class 0..n_cls
            rep = {}
            for x, c in enumerate(byte_to_cls):
                if c not in rep:
                    rep[c] = x
            for q, f, left in need_tcam:
                succ = [f[rep[c]] if c in rep else None for c in range(size)]
                head = t.node(dst=T.TO_S)
                t.dispatch(head, mode, cb, succ)
                t.when(root[q], (T.JZ,), (head, left))
            deepest = max(deepest, 1)
        else:
            for q, f, left in need_tcam:
                head, depth = tree.node(f, list(range(256)), 0)
                deepest = max(deepest, depth)
                t.when(root[q], (T.JZ,), (head, left))

    chains.build()
    return t, deepest, rules


def generate(bus, model):
    t, deepest, rules = _build(bus, model, use_tcam=True)
    packed = t.pack()
    if rules is not None and packed.top > T.DEPTH:
        # Rare case: many distinct TCAM jump tables overflowed 512 entries; retry with Tree.
        t2, deepest2, _ = _build(bus, model, use_tcam=False)
        packed2 = t2.pack()
        if packed2.top < packed.top:
            t, deepest, rules, packed = t2, deepest2, None, packed2
    if CLOCK_KHZ // bus.serve < period(deepest):
        raise Refused("too fast", "a clone that decides a byte in %d tests serves SPI up to %d kHz, not %d"
                      % (deepest, most_khz(deepest), bus.serve))
    return {
        "entries": packed.top,
        "chain": deepest,
        "image": packed.image(OUTS, INS, in_d=IN_D, tcam=rules) if packed.top <= T.DEPTH else None,
        "differs": [
            "MISO, its release and the answer to CS follow the pins through the chip's synchroniser and one or "
            "more entries, where the device had its own delay",
            "a deselect shorter than %d clocks can be missed" % (deepest + 3),
            "nRESET on P4 resets the clone whenever CS is high (or halted)",
        ],
    }
