# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The baselines are fair and the scores mean something: the passive learner does learn when the recording
holds the machine, a wrong model scores worse, and only the test lines that need hidden behaviour count as
hidden."""

import copy
import unittest

from common import Ideal, learn
from learn.lab import devices, judge, passive


class Baselines(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = devices.thermometer()
        cls.result = learn(cls.table)
        cls.truth = judge.TRUTH[cls.table.bus](cls.table)
        cls.minimal = judge.Minimal(cls.truth)
        cls.view = passive.View(cls.truth, cls.result["model"])

    def test_passive_learns_exactly_from_a_recording_as_long_as_the_questions(self):
        sessions = passive.recording("thermometer", self.truth, 23000, 1)
        learned = passive.Merger(self.view, sessions).run(0)
        self.assertEqual(len(learned.rows), self.minimal.n)
        self.assertTrue(passive.exact(learned, self.minimal))

    def test_a_wrong_model_is_not_exact_and_predicts_worse(self):
        model = self.result["model"]
        wrong = copy.deepcopy(model)
        wrong["rows"][0] = [(s, to, 1 - out) for s, to, out in wrong["rows"][0]]
        unseen = passive.recording("thermometer", self.truth, 200, 7)
        good, bad = (passive.model_predictor(self.view, m) for m in (model, wrong))
        self.assertTrue(passive.exact(good, self.minimal))
        self.assertTrue(all(passive.scored(good, unseen)))
        self.assertFalse(passive.exact(bad, self.minimal))
        self.assertFalse(all(passive.scored(bad, unseen)))

    def test_only_the_lines_that_need_hidden_behaviour_count_as_hidden(self):
        for part, count in (("flash", 6), ("thermometer", 0)):
            table = getattr(devices, part)()
            test = passive.device_test(part, judge.TRUTH[table.bus](table))
            self.assertEqual(len(passive.needs_hidden(part, test)), count, part)
        table = devices.sensor()
        test = passive.device_test("sensor", judge.TRUTH[table.bus](table))
        self.assertTrue(passive.needs_hidden("sensor", test))

    def test_stray_bytes_may_be_hidden_ones(self):
        class Part:
            documented = tuple(x for x in range(256) if x != 0x5E)
            hidden = (0x5E,)

        self.assertEqual(passive.undocumented(passive.random.Random(1), Part), 0x5E)

    def test_questions_at_random_learn_a_small_device(self):
        found = passive.at_random(Ideal(self.table), {"bus": self.table.bus}, 140000)
        self.assertEqual(found["reason"], "")
        self.assertTrue(passive.exact(passive.model_predictor(self.view, found["model"]), self.minimal))


if __name__ == "__main__":
    unittest.main()
