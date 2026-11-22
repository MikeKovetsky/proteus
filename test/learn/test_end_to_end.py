# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""End to end, on RTL: learn in the jail through the pins, judge, then put the clone in the device's place in
front of the original controller and the test written for the device. A few minutes."""

import unittest
from concurrent.futures import ThreadPoolExecutor

import common  # noqa: F401
from learn.lab import board, cases, judge, replace

JOBS = 3


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        board.build()
        every = cases.DEMONSTRATION + cases.CONTROLS
        with ThreadPoolExecutor(JOBS) as pool:
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

    def test_each_clone_replaces_its_device(self):
        with replace.Pair() as pair:
            for case in cases.DEMONSTRATION:
                image = self.done[case.name][0]["image"]
                found = replace.replacement(pair, case.name, image, count=100, jobs=JOBS)
                with self.subTest(case.name):
                    self.assertTrue(found["replaced"], found)
                    self.assertEqual(found["problems"], [])


if __name__ == "__main__":
    unittest.main()
