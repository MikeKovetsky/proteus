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

FAILING_BENCH = """
def fail(cls):
    if %s:
        cls.run = lambda self, *args: Result(self.name, False, "planted")
Bench.__init_subclass__ = classmethod(fail)
"""
STUCK_CHIP = ("bakeoff/cand_table/chip.py", """
TableChip.step = lambda self, pins: (0, 0)
""")
EVERY_BENCH_FAILS = ("bakeoff/harness/sim.py", FAILING_BENCH % "True")  # with one, the self-test takes 20 seconds
ONE_BENCH_FAILS = ("bakeoff/harness/sim.py", FAILING_BENCH % 'cls.name == "ws2812_tx"')
# The fault planted for each Python suite, and what it prints then. A crash exits 1 as well, and would prove nothing.
PYTHON_FAULTS = (
    (STUCK_CHIP, r"FAILED \(failures="),
    (EVERY_BENCH_FAILS, r"checks, [1-9]\d* failed"),
    (STUCK_CHIP, r"selfcheck: \d+ problems"),
    (ONE_BENCH_FAILS, r": [1-9]\d* of \d+ benches pass"),  # --strict must fail when only one does
    (STUCK_CHIP, r"uart_8e1: \d+ problems"),
)

# Stands in for Verilator: the program it "builds" exits with ON_RTL for the RTL, ON_FIRST_MUTANT for the first
# mutant and 1 (killed) for every other mutant.
VERILATOR = r"""#!/bin/sh
while [ $# -gt 1 ] && [ "$1" != -o ]; do shift; done
case "$2" in */mutants/m00/*) status=$ON_FIRST_MUTANT ;; */mutants/*) status=1 ;; *) status=$ON_RTL ;; esac
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
        self.assertEqual(len(PYTHON_FAULTS), len(PYTHON_SUITES), "a new suite needs its fault here")
        for command, ((path, fault), verdict) in zip(PYTHON_SUITES, PYTHON_FAULTS):
            with self.subTest(command):
                self.write(path, fault, "a")
                done = self.run_as_ci_does(command)
                shutil.copyfile(os.path.join(fakes.ROOT, path), os.path.join(self.root, path))
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
        # (lockstep's exit status on the RTL, on the first mutant, whether CI's command may pass)
        for on_rtl, on_first_mutant, may_pass in ((0, 1, True), (1, 1, False), (0, 0, False)):
            with self.subTest(on_rtl=on_rtl, on_first_mutant=on_first_mutant):
                shutil.rmtree(os.path.join(self.root, "test", "lockstep", "build"), ignore_errors=True)
                self.write("test/lockstep/build/traces/planted.trace", "")
                done = self.run_as_ci_does(MAKE_SUITE, PATH=path, PY="true", ON_RTL=str(on_rtl),
                                           ON_FIRST_MUTANT=str(on_first_mutant))
                self.assertEqual(done.returncode == 0, may_pass, done.stdout[-2000:])


if __name__ == "__main__":
    unittest.main()
