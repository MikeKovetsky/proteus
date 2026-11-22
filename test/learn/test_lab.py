# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The lab itself: the board's device shells, the real query engines and the judge."""

import random
import unittest

from common import Ideal, operations
from learn.host import session
from learn.lab import board, cloning, devices, judge, replace

LIKELY = {
    "flash": (0x04, 0x05, 0x06, 0x5E, 0x66, 0x99, 0x9F, 0xAB, 0xB9, 0xE7, 0, 256, 257),
    "sensor": (0, 1, 2, 15, 0x7E, 0xC3, 256 + 0x96, 256 + 0x97, 256 + 0x9E, 256 + 0x9F, 512, 513, 514),
    "thermometer": (0, 1, 2, 256 + 0x90, 256 + 0x91, 512, 513, 514),
}
PARTS = ("flash", "sensor", "thermometer")


def words(table, count, seed):
    truth, rng = judge.TRUTH[table.bus](table), random.Random(seed)
    for _ in range(count):
        state, word, wanted = (0, 0), [], []
        for _ in range(rng.randint(1, 16)):
            allowed = truth.symbols[state[1]]
            likely = [s for s in LIKELY[table.name] if s in allowed]
            s = rng.choice(likely) if likely and rng.random() < 0.8 else rng.choice(allowed)
            state, out = truth.step(state, s)
            word.append(s)
            wanted.append(out)
        yield tuple(word), wanted


def engine(table):
    told = session.settings({"bus": table.bus})
    return session.BUSES[table.bus](told)


class Board(unittest.TestCase):
    def test_the_shells_behave_as_their_tables(self):
        for name in PARTS:
            table = getattr(devices, name)()
            with board.Board(table.bus) as on:
                on.plug(table)
                for word, wanted in words(table, 250, 1):
                    self.assertEqual(judge.play(on, 1000 if table.bus == "spi" else 400, word), wanted, word)

    def test_the_query_engines_answer_as_the_ideal_link(self):
        for name in PARTS:
            table = getattr(devices, name)()
            ideal, bus = Ideal(table), engine(table)
            for rate in sorted(bus.ENGINES):
                with board.Board(table.bus, rate) as on:
                    on.plug(table)
                    self.assertTrue(on.load(bus.ENGINES[rate]))
                    for word, wanted in words(table, 120, rate):
                        data = bus.encode(word, (0,) if len(word) % 5 == 0 else (), 100)
                        got, errors = on.exchange(data, len(word))
                        self.assertEqual((list(got), errors), (wanted, b""), (name, rate, word))
                        self.assertEqual(got, ideal.exchange(data, len(word))[0])

    def test_the_shells_agree_with_independent_ones_in_icarus(self):
        """board.cpp under Verilator against lab/pair/shells.py under Icarus, through the real query engines."""
        with replace.Pair() as pair:
            for name, count in (("flash", 80), ("sensor", 30)):
                table, queries = getattr(devices, name)(), []
                bus = engine(table)
                with board.Board(table.bus) as on:
                    on.plug(table)
                    on.load(bus.image())
                    for word, _ in words(table, count, 7):
                        data = bus.encode(word)
                        self.assertEqual(len(list(operations(table.bus, data))), len(word) + 1)
                        queries.append((data, len(word), on.exchange(data, len(word))[0].hex()))
                run = pair.run("replay-" + name, {
                    "bus": table.bus, "part": name, "answers_with": "device", "clock_ps": 16668,
                    "engine": bus.image().hex(), "queries": [(data.hex(), size) for data, size, _ in queries]})
                self.assertEqual(run["answers"], [answer for _, _, answer in queries], name)
                self.assertEqual(run["faults"], [])

    def test_the_i2c_clone_follows_nreset(self):
        table = devices.sensor()
        _, result = cloning.generate(table, 400)
        with board.Board("i2c") as on:
            self.assertTrue(on.load(result["image"]))
            for word, wanted in words(table, 150, 3):  # play pulses nRESET and never loads again
                self.assertEqual(judge.play(on, 400, word), wanted, word)


class Judge(unittest.TestCase):
    def setUp(self):
        self.table = devices.flash()
        _, self.result = cloning.generate(self.table, 2000)

    def verdict(self, table, result):
        with board.Board(table.bus) as on:
            return judge.judge(on, table, result)["verdict"]

    def test_a_right_copy_is_exact(self):
        self.assertEqual(self.verdict(self.table, self.result), judge.EXACT)

    def test_a_wrong_image_is_a_wrong_copy(self):
        for compared, byte in ((False, 0x56), (True, 0x5E)):
            image = replace.broken(self.result["image"], byte, compared)
            self.assertEqual(self.verdict(self.table, dict(self.result, image=image)), judge.WRONG_CLONE)

    def test_a_wrong_model_inside_the_bound_is_a_broken_guarantee(self):
        rows = [list(row) for row in self.result["model"]["rows"]]
        for row in rows:
            if len(row) > 1:
                s, to, out = row[-1]
                row[-1] = (s, to, out ^ 1)
                break
        model = dict(self.result["model"], rows=rows)
        self.assertEqual(self.verdict(self.table, dict(self.result, model=model)), judge.BROKEN)

    def test_outside_the_bound_it_takes_a_warning_to_be_honest(self):
        blind = cloning.generate(cloning.two_byte(1, 1), 2000)[1]
        self.assertEqual(self.verdict(self.table, blind), judge.MISSED)
        self.assertEqual(self.verdict(self.table, dict(blind, warnings=[])), judge.SILENT)

    def test_a_refusal_with_an_image_is_silent(self):
        self.assertEqual(self.verdict(self.table, dict(self.result, ok=False)), judge.SILENT)

    def test_a_crash_is_an_error_and_not_a_refusal(self):
        crash = {"ok": False, "code": "error", "image": None, "model": None, "stats": {}}
        self.assertEqual(self.verdict(self.table, crash), judge.ERROR)


if __name__ == "__main__":
    unittest.main()
