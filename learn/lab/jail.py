# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Run the host where the chip is its only way to the device.

This side holds the board, which holds the device. The host's side holds a copy of learn/host and a pipe
(jail_child.py). Two requests cross it: load an image, exchange bytes through the chip's host link."""

import ast
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HOST = Path(__file__).resolve().parent.parent / "host"
CHILD = Path(__file__).resolve().parent / "jail_child.py"
PACKAGE = "learn_host"
LEN = struct.Struct("<I")
MOST = 1 << 26  # the longest message taken from the host
LIMIT_S = 1800


def failed(reason):
    return {"ok": False, "code": "error", "reason": reason, "image": None, "model": None, "stats": {},
            "guarantee": [], "warnings": [], "differs": [], "told": None, "wiring": "", "needs": []}


def learn(board, told, source=HOST, record=None, limit_s=LIMIT_S):
    """What the host returns, with the chip's time and this computer's added. `record` collects all traffic."""
    with tempfile.TemporaryDirectory(prefix="learn-host-") as root:
        package = Path(root).resolve() / PACKAGE
        package.mkdir()
        for path in Path(source).glob("*.py"):
            (package / path.name).write_bytes(path.read_bytes())
        child = package.parent / CHILD.name
        child.write_bytes(CHILD.read_bytes())
        began, clocks = time.time(), board.cycle
        process = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(child)], cwd=root, env={},
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   close_fds=True)
        timer = threading.Timer(limit_s, process.kill)
        timer.start()
        try:
            result = serve(board, process, repr(told).encode("utf-8"), record)
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.communicate()
    result["chip_ms"] = (board.cycle - clocks) / 60000.0
    result["wall_s"] = time.time() - began
    return result


def serve(board, process, told, record):
    out, into = process.stdout, process.stdin

    def reply(body):
        if record is not None:
            record.append((">", body))
        into.write(LEN.pack(len(body)) + body)
        into.flush()

    try:
        reply(told)
        while True:
            head = out.read(4)
            if len(head) < 4:
                return failed("the host process ended without an answer")
            size = LEN.unpack(head)[0]
            if not 0 < size <= MOST:
                return failed("the host sent a malformed message")
            body = out.read(size)
            tag, parts, at = body[:1], [], 1
            while at < len(body):
                count = LEN.unpack(body[at:at + 4])[0]
                parts.append(body[at + 4:at + 4 + count])
                at += 4 + count
            if record is not None and tag in (b"L", b"X"):
                record.append(("<", body))
            if tag == b"L" and len(parts) == 1:
                reply(b"\x01" if board.load(parts[0]) else b"\x00")
            elif tag == b"X" and len(parts) == 2 and len(parts[1]) == 4:
                got, errors = board.exchange(parts[0], min(LEN.unpack(parts[1])[0], MOST))
                data = got or b""
                reply((b"\x00" if got is None else b"\x01") + LEN.pack(len(data)) + data + errors)
            elif tag == b"R" and len(parts) == 1:
                result = ast.literal_eval(parts[0].decode("utf-8"))  # data, never code
                return result if isinstance(result, dict) and "ok" in result else failed("no result")
            elif tag == b"F" and len(parts) == 1:
                return failed("the host process failed: " + parts[0].decode("utf-8", "replace"))
            else:
                return failed("the host asked for something that does not exist")
    except (OSError, ValueError, SyntaxError, struct.error) as fault:
        return failed("the host process broke off (%s)" % fault)
