# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Table generation: the packed table does what the graph says, and its fields are the compiler's fields."""

import copy
import random
import unittest

from common import ROOT
from bakeoff.cand_table import entry as E
from learn.host import table as T
from learn.programs import build

ROUTING_A = 0x1FF | 15 << 9 | 15 << 13


def graph(seed):
    rng, t = random.Random(seed), T.Table()
    count = rng.randint(3, 90)
    shared = [rng.randrange(count) for _ in range(4)]  # targets that many share, as in a clone
    ids = []
    for _ in range(count):
        gated = rng.random() < 0.15
        ids.append((t.node(pin=(rng.randrange(4), rng.choice((1, 2, 3, 4, 7))) if gated or rng.random() < 0.3
                           else None,
                           gate=rng.choice((1, 2)) if gated else 0, imm=rng.randrange(6), kj=rng.choice((0, 3)),
                           shift=int(rng.random() < 0.1), din=rng.randrange(2)), gated))

    def pick():
        return rng.choice(shared) if rng.random() < 0.5 else rng.randrange(count)

    for node, gated in ids:
        conds = rng.sample((T.A, T.B, T.C, T.KZ, T.JZ, T.EQUAL), 2)
        roll = rng.random()
        if roll < 0.35:
            t.when(node, conds[:1], (pick(), pick()))
        elif roll < 0.6:
            four = [pick(), pick(), pick(), pick()]
            if not gated and rng.random() < 0.3:
                four[rng.randrange(4)] = None
            t.when(node, conds, four)
        elif gated:
            t.when(node, conds[:1], (pick(), pick()))
        else:
            t.go(node, pick())
    plain = [node for node, gated in ids if not gated]
    for _ in range(rng.randint(0, 3) if plain else 0):
        back, target = pick(), pick()
        t.go(rng.choice(plain), t.call(target, back))
    return t


def differs(wanted, words):
    """Walk the graph and the packed words together. None, or where they part."""
    seen, todo = set(), [(0, 0)]
    while todo:
        pair = todo.pop()
        if pair in seen:
            continue
        seen.add(pair)
        node, addr = wanted.nodes[pair[0]], pair[1]
        if addr >= len(words):
            return "address %d is past the table" % addr
        a, b = words[addr]
        base, ca, cb = a & 0x1FF, a >> 9 & 15, a >> 13 & 15
        if a & ~ROUTING_A != node.word_a:
            return "word A's operations at %d" % addr
        if node.gated and ca != node.conds[0]:
            return "the gate at %d is another condition" % addr
        if node.back is not None:
            if b & ~0x1FF != node.word_b:
                return "word B at %d" % addr
            todo.append((node.back, b & 0x1FF))
        elif b != node.word_b:
            return "word B at %d" % addr
        if not node.succ:
            continue
        flags = sorted(set(node.conds) | ({ca, cb} - {0}))
        for bits in range(1 << len(flags)):
            true = {flag: bits >> i & 1 for i, flag in enumerate(flags)}
            index = sum(true[c] << i for i, c in enumerate(node.conds)) if len(node.succ) > 1 else 0
            to = node.succ[index]
            if to is not None:
                todo.append((to, base | true.get(ca, 0) | true.get(cb, 0) << 1))
    return None


class Packing(unittest.TestCase):
    def test_the_packed_table_does_what_the_graph_says(self):
        packed = 0
        for seed in range(1500):
            t = graph(seed)
            wanted = copy.deepcopy(t)
            placed = t.pack()
            if placed.top <= T.DEPTH:
                packed += 1
                self.assertIsNone(differs(wanted, placed.words()), "seed %d" % seed)
        self.assertGreater(packed, 1400)

    def test_a_wrong_pairing_is_seen(self):
        places = T.PLACES
        try:
            T.PLACES = ((0, 3), (1, 2))  # 1 and 2 cannot be one condition's two successors
            caught = 0
            for seed in range(100):
                t = graph(seed)
                wanted = copy.deepcopy(t)
                placed = t.pack()
                if placed.top <= T.DEPTH and differs(wanted, placed.words()):
                    caught += 1
            self.assertGreater(caught, 0)
        finally:
            T.PLACES = places

    def test_fields_are_entry_pack(self):
        rng = random.Random(1)
        for _ in range(3000):
            fields = dict(p0=rng.randrange(4), p0op=rng.randrange(1, 8), gate=rng.randrange(3),
                          din=rng.randrange(4), imm=rng.randrange(1 << 16), src=rng.randrange(7),
                          dst=rng.randrange(8), shift=rng.randrange(2), nop=rng.randrange(4), kj=rng.randrange(8),
                          tload=rng.randrange(2))
            t = T.Table()
            node = t.nodes[t.node(pin=(fields["p0"], fields["p0op"]), **{
                key: fields[key] for key in fields if key not in ("p0", "p0op")})]
            self.assertEqual((node.word_a, node.word_b), E.pack(dict(E.blank(), **fields)))

    def test_codes_are_the_compilers(self):
        self.assertEqual(
            (T.A, T.B, T.C, T.TZ, T.KZ, T.JZ, T.D, T.EQUAL),
            (E.C_A, E.C_B, E.C_C, E.C_TZ, E.C_KZ, E.C_JZ, E.C_D, E.C_EQ))
        self.assertEqual((T.LOW, T.HIGH, T.RELEASE, T.DATA, T.OPEN_DRAIN),
                         (E.P_LOW, E.P_HIGH, E.P_RELEASE, E.P_DATA, E.P_OD))
        self.assertEqual((T.TO_S, T.PUSH2, T.LINK, T.RETURN, T.DIN_A, T.DIN_B, T.DEPTH),
                         (E.DST_S, E.DST_PUSH2, E.DST_LINK, E.N_RETURN, E.DIN_A, E.DIN_B, E.DEPTH))
        self.assertEqual((T.J_LOAD, T.K_DEC, T.K_LOAD), (E.LOAD, 3 * E.DEC, 3 * E.LOAD))

    def test_the_image_is_the_loaders_format(self):
        t = graph(7)
        placed = t.pack()
        configs, words = E.parse_image(placed.image((2, 0, 1, 4), (0, 1, 3)))
        self.assertEqual((configs[0].out, configs[0].inp, configs[0].msb), ([2, 0, 1, 4], [0, 1, 3], 1))
        self.assertEqual(words, placed.words())

    def test_the_query_engines_are_the_compiled_sources(self):
        self.assertEqual((ROOT / "learn" / "host" / "probes.py").read_text(encoding="utf-8"), build.render(),
                         "run python3 -m learn.programs.build")


if __name__ == "__main__":
    unittest.main()
