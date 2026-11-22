# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The process that the host runs in. learn/lab/jail.py copies this file and learn/host, and nothing else, to an
empty folder and starts it there with `python -I -S -B`. It clears its environment first, since a wrapper
script round the interpreter can refill the empty one it was started with.

Before any host code runs, an audit hook allows only loading that copy. Before the host is called, it refuses
every audited event: open, import, exec, socket, subprocess and the rest. What is left is the pipe to the
parent, which answers two requests: load an image, exchange bytes with the chip.

A CPython audit hook is no wall against hostile native code. It shows that this host's code, whose imports
test/learn also checks, has no path to the device but the chip."""

import os
import struct
import sys
from ast import literal_eval

PACKAGE = "learn_host"
LEN = struct.Struct("<I")
LOADING, LOCKED = 1, 2


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    inside = root + os.sep
    real_in, real_out = sys.stdin.buffer, sys.stdout.buffer
    leave = os._exit

    def receive():
        head = real_in.read(4)
        if len(head) < 4:
            leave(3)
        return real_in.read(LEN.unpack(head)[0])

    def send(tag, *parts):
        body = tag + b"".join(LEN.pack(len(part)) + part for part in parts)
        real_out.write(LEN.pack(len(body)) + body)
        real_out.flush()

    class Link:
        def load(self, image):
            send(b"L", bytes(image))
            return receive() == b"\x01"

        def exchange(self, data, want):
            send(b"X", bytes(data), LEN.pack(want))
            got = receive()
            size = LEN.unpack(got[1:5])[0]
            return (got[5:5 + size] if got[0] else None), got[5 + size:]

    os.environ.clear()
    getattr(sys.modules.get("posix"), "environ", {}).clear()  # the module keeps a copy of its own
    told = literal_eval(receive().decode("utf-8"))
    state = [LOADING]

    def ours(path):
        return isinstance(path, str) and (path == root or path.startswith(inside))

    def audit(event, args):
        if state[0] == LOADING:
            if event == "import" and args[0].split(".")[0] == PACKAGE:
                return
            if event == "open" and ours(args[0]) and args[1] in ("r", "rb"):
                return
            if event in ("os.listdir", "os.scandir") and ours(args[0]):
                return
            if event == "compile" and ours(args[1]):
                return
            if event == "exec" and ours(args[0].co_filename):
                return
            if event == "marshal.loads" or event.startswith("sys._getframe"):
                return
        raise PermissionError("the host may not %s" % event)

    sys.path[:] = [root]
    sys.addaudithook(audit)
    try:
        session = __import__(PACKAGE + ".session", None, None, ["run"])
        for name in ("clone_spi", "clone_i2c"):  # imported on use by the adapters, so before the lock
            __import__(PACKAGE + "." + name, None, None, ["generate"])
        state[0] = LOCKED
        send(b"R", repr(session.run(told, Link())).encode("utf-8"))
    except BaseException as fault:
        send(b"F", ("%s: %s" % (type(fault).__name__, fault)).encode("utf-8"))
    leave(0)  # the interpreter's own shutdown raises audit events


if __name__ == "__main__":
    main()
