# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Each suite CI runs can fail it: on a copy with a planted fault, CI's own command exits nonzero."""
import os
import shutil
import subprocess
import tempfile
import unittest

import fakes
from test_workflows import SUITES

*PYTHON_SUITES, MAKE_SUITE = SUITES

# A chip that never drives a pin, and benches that fail whatever they are given. Both fail fast.
FAULTS = {
    "bakeoff/cand_table/chip.py": """
TableChip.step = lambda self, pins: (0, 0)
""",
    "bakeoff/harness/sim.py": """
def fail_every_run(cls):
    cls.run = lambda self, *args: Result(self.name, False, "planted")
Bench.__init_subclass__ = classmethod(fail_every_run)
""",
}
# What each Python suite prints when it finds a fault. A crash exits 1 as well, and would prove nothing.
VERDICTS = (r"FAILED \(failures=", r"checks, [1-9]\d* failed", r"selfcheck: \d+ problems", r": 0 of 14 benches pass",
            r"uart_8e1: \d+ problems")

# Stands in for Verilator: the program it "builds" exits with ON_RTL, or with ON_MUTANT for a mutant's build.
VERILATOR = r"""#!/bin/sh
while [ $# -gt 1 ] && [ "$1" != -o ]; do shift; done
case "$2" in */mutants/*) status=$ON_MUTANT ;; *) status=$ON_RTL ;; esac
printf '#!/bin/sh\nexit %s\n' "$status" > "$2"
chmod +x "$2"
"""


class SuitesCanFail(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        skip = shutil.ignore_patterns("build", "sim_build", "__pycache__", "*.fst", "results.xml")
        for folder in ("bakeoff", "docs", "src", "test"):
            shutil.copytree(os.path.join(fakes.ROOT, folder), os.path.join(self.root, folder), ignore=skip)

    def write(self, path, text, mode="w"):
        path = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode, encoding="utf8") as f:
            f.write(text)
        return path

    def run_as_ci_does(self, command, **env):
        base = {key: value for key, value in os.environ.items() if key not in ("MAKEFLAGS", "MFLAGS")}
        return subprocess.run(["bash", "-e", "-c", command], cwd=self.root, env=dict(base, **env),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)

    def test_each_python_suite_fails_on_a_fault(self):
        for path, fault in FAULTS.items():
            self.write(path, fault, "a")
        self.assertEqual(len(VERDICTS), len(PYTHON_SUITES), "a new suite needs its verdict here")
        for command, verdict in zip(PYTHON_SUITES, VERDICTS):
            with self.subTest(command):
                done = self.run_as_ci_does(command)
                self.assertRegex(done.stdout, verdict)
                self.assertEqual(done.returncode, 1, done.stdout[-2000:])

    def test_make_fails_if_lockstep_fails_or_a_mutant_survives(self):
        # The two Makefiles and mutants.py run as they are. Verilator and cocotb's makefiles are stand-ins and
        # PY=true records no traces, so this needs neither tool and takes seconds, not minutes.
        tools = os.path.join(self.root, "tools")
        self.write("tools/Makefile.sim", "")
        for name, script in (("verilator", VERILATOR), ("cocotb-config", "#!/bin/sh\necho '%s'\n" % tools)):
            os.chmod(self.write("tools/" + name, script), 0o755)
        path = tools + os.pathsep + os.environ["PATH"]
        # (lockstep's exit status on the RTL, on every mutant, whether CI's command may pass)
        for on_rtl, on_mutant, may_pass in ((0, 1, True), (1, 1, False), (0, 0, False)):
            with self.subTest(on_rtl=on_rtl, on_mutant=on_mutant):
                shutil.rmtree(os.path.join(self.root, "test", "lockstep", "build"), ignore_errors=True)
                self.write("test/lockstep/build/traces/planted.trace", "")
                done = self.run_as_ci_does(MAKE_SUITE, PATH=path, PY="true", ON_RTL=str(on_rtl),
                                           ON_MUTANT=str(on_mutant))
                self.assertEqual(done.returncode == 0, may_pass, done.stdout[-2000:])


if __name__ == "__main__":
    unittest.main()
