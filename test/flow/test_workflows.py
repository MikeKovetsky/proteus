# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The CI wiring that the layout gates and the public viewer rely on (.github/workflows), read as text."""
import os
import re
import unittest

import fakes

WORKFLOWS = os.path.join(fakes.ROOT, ".github", "workflows")
SUITES = (
    "python3 -m bakeoff.cand_table.test_compiler",
    "python3 -m bakeoff.harness.selftest",
    "python3 -m bakeoff.cand_table.selfcheck",
    "python3 -m bakeoff.run --candidate cand_table --strict",
    "python3 docs/examples/run_uart_8e1.py",
    "make -C test mutants",  # runs lockstep first
)


def workflows():
    for name in sorted(os.listdir(WORKFLOWS)):
        if not name.endswith((".yaml", ".yml")):  # GitHub reads only these, and macOS leaves .DS_Store about
            continue
        with open(os.path.join(WORKFLOWS, name), encoding="utf8") as f:
            yield name, f.read()


def jobs(workflow):
    """Each job's text, by name."""
    parts = re.split(r"^  ([\w-]+):\n", workflow.split("\njobs:\n", 1)[1], flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def steps(job):
    return re.split(r"^      - ", job.split("    steps:\n", 1)[1], flags=re.M)[1:]


class Workflows(unittest.TestCase):
    def test_layout_report_runs_after_a_failed_build_too(self):
        gds = jobs(dict(workflows())["gds.yaml"])["gds"]
        gds_steps = steps(gds)
        build = next(i for i, step in enumerate(gds_steps) if "uses: TinyTapeout/tt-gds-action@" in step)
        report = next(i for i, step in enumerate(gds_steps) if "run: python3 .github/scripts/layout_report.py" in step)
        self.assertLess(build, report)
        self.assertRegex(gds_steps[report], r"(?m)^        if: always\(\)$")
        self.assertRegex(gds_steps[report], r"(?m)^        run: python3 \.github/scripts/layout_report\.py runs/wokwi$")
        self.assertNotRegex(gds, r"(?m)^ *continue-on-error:")

    def test_only_main_deploys_the_viewer(self):
        deployers = 0
        for name, text in workflows():
            self.assertNotIn("pages: write", text.split("\njobs:\n")[0], "%s gives every job Pages" % name)
            for job, body in jobs(text).items():
                if not re.search(r"pages: write|deploy-pages|tt-gds-action/viewer", body):
                    continue
                deployers += 1
                self.assertRegex(body, r"(?m)^    if: github\.ref == 'refs/heads/main'$", "%s: %s" % (name, job))
                # GitHub applies the github-pages environment's main-only rule just to jobs that name it.
                self.assertRegex(body, r"(?m)^    environment: github-pages$", "%s: %s" % (name, job))
        self.assertEqual(deployers, 1)

    def test_ci_runs_these_tests(self):
        test = jobs(dict(workflows())["test.yaml"])["test"]
        self.assertRegex(test, r"(?m)^        run: python3 -m unittest discover -s test/flow\b")
        self.assertNotRegex(test, r"(?m)^ *continue-on-error:")

    def test_every_push_runs_the_suites(self):
        workflow = dict(workflows())["test.yaml"]
        self.assertRegex(workflow, r"(?m)^on: \[push\b")
        suites = jobs(workflow)["suites"]
        for command in SUITES:
            self.assertRegex(suites, r"(?m)^        run: %s$" % re.escape(command))
        # Nothing may skip a suite or let one fail quietly.
        self.assertNotRegex(suites, r"(?m)^[ -]*(if|continue-on-error|shell):")


if __name__ == "__main__":
    unittest.main()
