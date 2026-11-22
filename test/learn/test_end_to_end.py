# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""End to end, on RTL: learn in the jail through the pins, judge, then put the clone in the device's place in
front of the original controller and the test written for the device. A few minutes."""

import unittest
from concurrent.futures import ThreadPoolExecutor

import common  # noqa: F401
from learn.lab import board, cases, cloning, judge, measure, replace

# The first feasibility probe's two largest machines, which didn't fit there, carried to SPI with their inputs told
PROBES = tuple(cases.Case("the probe's %d" % raw, "spi", lambda raw=raw: cloning.probes_machine(raw),
                          {"serve": 2000, "inputs": measure.TWO_BYTE_INPUTS}, judge.EXACT, "ok", "")
               for raw in (64, 100))


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        board.build()
        every = cases.DEMONSTRATION + cases.CONTROLS + PROBES
        with ThreadPoolExecutor(board.jobs()) as pool:
            cls.done = dict(zip([case.name for case in every], pool.map(cases.run, every)))

    def test_every_case_comes_to_what_it_must(self):
        for case in cases.DEMONSTRATION + cases.CONTROLS:
            result, report = self.done[case.name]
            with self.subTest(case.name):
                self.assertNotIn(report["verdict"], judge.FAILURES, result["reason"])
                self.assertTrue(report["expected"], "%s, %s: %s" % (report["verdict"], result["code"],
                                                                    result["reason"]))
                if result["ok"]:
                    self.assertTrue(result["warnings"] and result["guarantee"], "a copy with no warning")
                else:
                    self.assertIsNone(result["image"], "a refusal with a program")

    def test_the_hidden_features_were_found(self):
        for name, hidden in (("flash", (0x5E, 0xE7)), ("sensor", (0x7E, 0xC3, 256 + 0x9F))):
            self.assertLessEqual(set(hidden), set(self.done[name][0]["model"]["alphabet"]), name)

    def test_the_first_probes_two_largest_are_learned_through_the_pins(self):
        for case, entries in zip(PROBES, (300, 461)):
            result, report = self.done[case.name]
            self.assertEqual((report["verdict"], result["stats"].get("entries")), (judge.EXACT, entries),
                             "%s: %s" % (case.name, result["reason"]))

    def test_each_clone_replaces_its_device(self):
        with replace.Pair() as pair:
            for case in cases.DEMONSTRATION:
                image = self.done[case.name][0]["image"]
                found = replace.replacement(pair, case.name, image, count=100)
                with self.subTest(case.name):
                    self.assertTrue(found["replaced"], found)
                    self.assertEqual(found["problems"], [])


if __name__ == "__main__":
    unittest.main()
