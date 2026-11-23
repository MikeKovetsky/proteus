# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The evaluation can't be bent afterwards: a freeze's host can be run again, a changed host is noticed, the
devices follow from the seeds, and a failure can't drop out of the summary."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import common  # noqa: F401  (it puts the repo on the path)
from learn.lab import families, judge


class Families(unittest.TestCase):
    def test_the_first_freeze_can_be_run_again(self):
        """The first blind run stays reproducible: its host comes back out of git, as it was frozen."""
        with tempfile.TemporaryDirectory() as root:
            host = families.first_host(root)
            self.assertTrue(families.frozen(root))
            self.assertEqual(families.which(root), "first freeze")
            self.assertTrue((host / "search.py").is_file())

    def test_a_changed_host_is_noticed(self):
        with tempfile.TemporaryDirectory() as root:
            families.first_host(root)
            path = Path(root) / "learn/host/search.py"
            path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            self.assertFalse(families.frozen(root))
            self.assertIsNone(families.which(root))
        with tempfile.TemporaryDirectory() as root:  # and a file added counts as a change
            for folder in families.FROZEN:
                shutil.copytree(str(families.ROOT / folder), str(Path(root) / folder))
            before = families.digest(root)
            (Path(root) / "learn/programs/extra.fsm").write_text("", encoding="utf-8")
            self.assertNotEqual(families.digest(root), before)

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

    def test_the_kept_result_has_every_device_once(self):
        kept = families.ROOT / "learn/results/families.jsonl"
        rows = [json.loads(line) for line in kept.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted((row["family"], row["i"]) for row in rows), sorted(families.tasks()))
        verdicts = [row["verdict"] for row in rows]  # what notes/learn-it-become-it.md says of them
        self.assertEqual([verdicts.count(v) for v in (judge.EXACT, judge.REFUSED, judge.MISSED)], [106, 15, 39])

    def test_a_crash_is_a_failure(self):
        row = families.one(("no such family", 0))
        self.assertEqual((row["verdict"], row["code"]), (judge.ERROR, "harness"))


if __name__ == "__main__":
    unittest.main()
