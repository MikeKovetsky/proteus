# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The evaluation can't be bent afterwards: the host is the frozen one, the devices follow from the seeds, and a
failure can't drop out of the summary."""

import shutil
import tempfile
import unittest
from pathlib import Path

import common  # noqa: F401  (it puts the repo on the path)
from learn.lab import families, judge


class Families(unittest.TestCase):
    def test_the_host_is_the_frozen_one(self):
        self.assertTrue(families.frozen(),
                        "learn/host or learn/programs changed after the freeze, so the evaluation is void")

    def test_a_changed_host_is_noticed(self):
        with tempfile.TemporaryDirectory() as root:
            for folder in families.FROZEN:
                shutil.copytree(str(families.ROOT / folder), str(Path(root) / folder))
            self.assertTrue(families.frozen(root))
            path = Path(root) / "learn/host/search.py"
            path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            self.assertFalse(families.frozen(root))

    def test_devices_follow_from_their_seeds(self):
        for family in families.FAMILIES:
            first, again, other = (families.device(family, i) for i in (0, 0, 1))
            whole = [(t.out, t.next, t.a, t.b, t.c, t.ack) for t in (first, again, other)]
            self.assertEqual(whole[0], whole[1], family)
            self.assertNotEqual(whole[0], whole[2], family)
            self.assertGreater(judge.Minimal(judge.TRUTH[first.bus](first)).n, 2, family)

    def test_every_device_is_run_and_every_failure_shows(self):
        tasks = families.tasks()
        self.assertEqual(len(set(tasks)), families.COUNT * len(families.FAMILIES))
        for failure in judge.FAILURES:
            row = {"family": "spi modes", "i": 3, "verdict": failure, "code": "ok", "reason": "planted"}
            lines = families.summary([row])
            self.assertEqual(lines[2].split()[-3], "1", failure)  # the family's FAILED column
            self.assertIn("planted", lines[-1])

    def test_a_crash_is_a_failure(self):
        row = families.one(("no such family", 0))
        self.assertEqual((row["verdict"], row["code"]), (judge.ERROR, "harness"))


if __name__ == "__main__":
    unittest.main()
