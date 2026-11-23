# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Host-side unit tests for `fpga/tang_nano_9k/pico2_host.py` (verifies MicroPython subset + protocol)."""
import ast
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pico2_host as ph  # noqa: E402


class FakePin:
    IN = 0
    OUT = 1
    PULL_UP = 2

    def __init__(self, num, mode=0, pull=None, value=0):
        self.num = num
        self.mode = mode
        self._val = value

    def value(self, v=None):
        if v is not None:
            self._val = 1 if v else 0
        return self._val


class FakeUART:
    def __init__(self, idx, baudrate=1350000, tx=None, rx=None):
        self.baudrate = baudrate
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    def any(self):
        return len(self.buf)

    def read(self):
        out = bytes(self.buf)
        self.buf.clear()
        return out


class TestPico2Host(unittest.TestCase):
    def test_micropython_syntax_and_zero_external_imports(self):
        tree = ast.parse((HERE / "pico2_host.py").read_text())
        allowed = {"json", "sys", "time", "machine"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, allowed)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn(node.module, allowed)

    def test_nibble_loader_and_send_frame_protocol(self):
        host = ph.Pico2Host(pin_cls=FakePin, uart_cls=FakeUART, drive_kill=True)
        # Simulate Proteus ready (ROOM=1, RUNNING=1)
        host.uo[4].value(1)  # ST_ROOM
        host.uo[6].value(1)  # ST_RUNNING

        resp = host.handle_line("LOAD 01020304")
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["running"])

        ping = host.handle_line("PING")
        self.assertEqual(ping["board"], "pico2-tang9k")
        self.assertTrue(ping["running"])

        # Loopback UART frame test
        res = host.handle_line("SEND_FRAME 4f01820a -1 0")
        self.assertTrue(res["ok"])
        self.assertEqual(res["tx_hex"], "4f01820a")

        # Software kill-switch drive
        k = host.handle_line("SET_KILL 0")
        self.assertEqual(k["kill_n"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
