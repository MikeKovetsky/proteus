# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The host has no path to a device but the chip. Its imports say so, the jail enforces it, and the traffic
shows it."""

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from common import ROOT, renumbered
from learn.lab import board, devices, jail

HOST = ROOT / "learn" / "host"
MODULES = {path.stem for path in HOST.glob("*.py")}
FORBIDDEN = {"open", "eval", "exec", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr",
             "breakpoint", "input", "print", "memoryview", "__builtins__", "help", "dir"}
DUNDERS = {"__init__", "__name__"}

# A host that tries to get out. Each line is an attempt: if it runs without an exception, it escaped.
ESCAPER = '''
TRIES = (
    ("read a file", "open(%(secret)r).read()"),
    ("read it by the os", "modules['posix'].open(%(secret)r, 0)"),
    ("read it raw", "modules['_io'].FileIO(%(secret)r).read()"),
    ("list a folder", "modules['posix'].listdir(%(folder)r)"),
    ("change folder", "modules['posix'].chdir(%(folder)r)"),
    ("start a shell", "modules['posix'].system('exit 0')"),
    ("import a library", "import_('mmap')"),
    ("import a socket", "import_('_socket')"),
    ("import ctypes", "import_('_ctypes')"),
    ("compile code", "compile('1', 'made up', 'eval')"),
)


def run(told, link):
    import_ = __import__
    modules = import_("sys").modules
    escaped = []
%(attempts)s
    return {"ok": False, "code": "probe", "escaped": escaped, "image": None, "model": None, "stats": {},
            "environment": sorted(modules["posix"].environ)}
'''


def escaper(secret, folder):
    """The module's source, with each attempt written out: eval would itself be caught, and prove nothing."""
    values = {"secret": str(secret), "folder": str(folder)}
    namespace = {}
    exec(ESCAPER.split("def run", 1)[0] % values, namespace)  # only the list of attempts
    lines = []
    for label, code in namespace["TRIES"]:
        lines += ["    try:", "        %s" % code, "        escaped.append(%r)" % label,
                  "    except Exception:", "        pass"]
    return ESCAPER % dict(values, attempts="\n".join(lines)), [label for label, _ in namespace["TRIES"]]


class Imports(unittest.TestCase):
    def parsed(self):
        for path in sorted(HOST.glob("*.py")):
            yield path.name, ast.parse(path.read_text(encoding="utf-8"))

    def test_the_host_imports_only_itself(self):
        for name, tree in self.parsed():
            for node in ast.walk(tree):
                self.assertNotIsInstance(node, ast.Import, "%s: import statement" % name)
                if isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.level, 1, "%s: from %s" % (name, node.module))
                    targets = [node.module] if node.module else [alias.name for alias in node.names]
                    self.assertLessEqual(set(targets), MODULES, name)

    def test_the_host_uses_no_door_out(self):
        for name, tree in self.parsed():
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, FORBIDDEN, name)
                elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                    self.assertIn(node.attr, DUNDERS, name)

    def test_the_host_keeps_to_micropythons_language(self):
        """Syntax only. It has not been run under MicroPython."""
        for name, tree in self.parsed():
            for node in ast.walk(tree):
                self.assertNotIsInstance(node, (ast.JoinedStr, ast.NamedExpr, ast.AsyncFunctionDef, ast.Yield,
                                                ast.AnnAssign), name)
                if isinstance(node, ast.Slice):
                    self.assertIsNone(node.step, "%s: a slice with a step" % name)
                if isinstance(node, ast.Attribute) and node.attr == "__init__" and isinstance(node.value, ast.Name):
                    # builtin classes there have no __init__ to name: super() is the way
                    self.assertNotIn(node.value.id, ("Exception", "object", "dict", "list", "tuple"), name)


class Jail(unittest.TestCase):
    def test_no_way_out(self):
        with tempfile.TemporaryDirectory() as folder:
            secret = Path(folder) / "truth"
            secret.write_text("the device's table")
            host = Path(folder) / "host"
            host.mkdir()
            source, labels = escaper(secret, folder)
            (host / "session.py").write_text(source)
            for name in ("__init__", "clone_spi", "clone_i2c"):
                (host / (name + ".py")).write_text("def generate(*args):\n    pass\n")
            with board.Board("spi") as on:
                result = jail.learn(on, {"bus": "spi"}, source=host)
            self.assertEqual(result["code"], "probe", result.get("reason"))
            self.assertEqual(result["escaped"], [])
            self.assertEqual(result["environment"], [])

            # The same attempts with no jail get out, so it is the jail that stopped them.
            script = ("import sys, json; g = {'__name__': 'x'}; exec(open(sys.argv[1]).read(), g); "
                      "print(json.dumps(g['run']({}, None)['escaped']))")
            free = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(host / "session.py")], cwd=folder,
                                  stdout=subprocess.PIPE, check=True)
            self.assertEqual(json.loads(free.stdout), labels)

    def test_no_way_out_while_loading(self):
        with tempfile.TemporaryDirectory() as folder:
            secret = Path(folder) / "truth"
            secret.write_text("the device's table")
            host = Path(folder) / "host"
            host.mkdir()
            (host / "__init__.py").write_text("STOLEN = open(%r).read()\n" % str(secret))
            (host / "session.py").write_text("def run(told, link):\n    pass\n")
            with board.Board("spi") as on:
                result = jail.learn(on, {"bus": "spi"}, source=host)
            self.assertEqual((result["ok"], result["code"]), (False, "error"))
            self.assertIn("may not open", result["reason"])

    def test_the_traffic_is_behaviour_and_nothing_else(self):
        """Two devices that behave alike and are built differently give the host byte-identical traffic."""
        told = {"bus": "spi", "budget": 120000}
        runs = []
        for table in (devices.flash(), renumbered(devices.flash()), devices.spi_table(Changed())):
            record = []
            with board.Board("spi") as on:
                on.plug(table)
                jail.learn(on, told, record=record)
            runs.append(record)
        self.assertGreater(len(runs[0]), 1000)
        self.assertEqual(runs[0], runs[1])
        self.assertNotEqual(runs[0], runs[2], "another device gives the same traffic, so it proves nothing")

        record = runs[0]
        self.assertEqual(record[0], (">", repr(told).encode()), "the host was sent more than what it is told")
        for i in range(1, len(record), 2):
            (way, asked), (back, reply) = record[i], record[i + 1]
            self.assertEqual((way, back), ("<", ">"))
            if asked[:1] == b"L":
                self.assertEqual(reply, b"\x01")
            else:
                wanted = int.from_bytes(asked[-4:], "little")
                self.assertEqual((asked[:1], reply[0], len(reply)), (b"X", 1, 5 + wanted))


class Changed(devices.Flash):
    def sends(self, state):
        sent = super().sends(state)
        return 0x15 if sent == 0x14 else sent


if __name__ == "__main__":
    unittest.main()
