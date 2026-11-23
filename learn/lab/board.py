# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The Python side of board.cpp: the Verilated chip on a board, built on first use.

Nothing here is the host's. The host gets two calls of it, `load` and `exchange`, through learn/lab/jail.py."""

import ctypes as C
import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
from collections import namedtuple
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BUILD = ROOT / "learn" / "build"
SOURCES = (
    "test/lint/waivers.vlt", "src/project.v", "src/protoemu_core.v", "src/protoemu_fifo.v", "src/protoemu_table.v",
)
OPTIONS = (
    "--cc", "--exe", "--build", "-O3", "--savable", "--no-timing", "--x-initial", "unique",
    "-Wno-STMTDLY", "--timescale", "1ns/1ps", "-DFUNCTIONAL", "--top-module", "tt_um_mikekovetsky_protoemu",
    "-CFLAGS", "-fPIC -O3", "-MAKEFLAGS", "OPT_FAST=-O3 OPT_SLOW=-O1 OPT_GLOBAL=-O2", "-LDFLAGS", "-shared",
)
CLOCKS_PER_US = 60
P0, P1, P2, P3, P4 = 1, 2, 4, 8, 16
# The pull-ups a bus has on a board, and the pins of it that rise slowly when they are let go
PULL = {"spi": P2 | P3 | P4, "i2c": P0 | P1 | P4}
RISE = {"spi": (0, 0), "i2c": (P0 | P1, 18)}  # 300 ns: Fast-mode's longest
DELAY = {"spi": 3, "i2c": 20}  # clocks from the edge to the device's answer: 50 ns, and 333 ns
KIND = {"spi": 1, "i2c": 2}
RECORD = struct.Struct("<QBBBB4x")

Pins = namedtuple("Pins", "cycle line chip_oe chip_out status contention chip_high dev_oe")


def jobs():
    """How many heavy processes may run at once: LEARN_JOBS, else 2. Never the machine's count of cores, since
    other work shares the machine. It changes no result."""
    told = os.environ.get("LEARN_JOBS", "2")
    if not told.isdigit() or int(told) < 1:
        raise ValueError("LEARN_JOBS is a whole number from 1, not %r" % told)
    return int(told)


def build():
    sources = [ROOT / name for name in SOURCES] + sorted((ROOT / "test" / "ihp_sram").glob("*.v"))
    sources.append(ROOT / "learn" / "lab" / "board.cpp")
    digest = hashlib.sha256(repr(OPTIONS).encode())
    for path in sources:
        digest.update(path.read_bytes())
    out = BUILD / ("board-%s.so" % digest.hexdigest()[:16])
    if out.exists():
        return out
    verilator = shutil.which("verilator")
    if not verilator:
        raise RuntimeError("no verilator on the path: source ~/oss-cad-suite/environment first")
    BUILD.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="board-", dir=str(BUILD)) as work:
        made = Path(work) / "board.so"
        command = [verilator, *OPTIONS, "-j", str(jobs()), "--Mdir", str(Path(work) / "obj"), "-o", str(made),
                   *map(str, sources)]
        done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        if done.returncode:
            raise RuntimeError("verilator failed:\n" + done.stdout[-4000:])
        os.replace(str(made), str(out))
    return out


@lru_cache(None)
def library():
    lib = C.CDLL(str(build()))
    board, u8, i32, u32, u64 = C.c_void_p, C.c_char_p, C.POINTER(C.c_int32), C.c_uint32, C.c_uint64
    for name, result, args in (
        ("new", board, [C.c_uint8, u32]),
        ("free", None, [board]),
        ("plug", C.c_int, [board, C.c_int, C.c_int, C.c_int, u8, u8, i32, u8, i32, i32, i32, i32, u32, u32, u32, u32]),
        ("load", C.c_int, [board, u8, C.c_int]),
        ("exchange", C.c_int, [board, u8, C.c_int, u8, C.c_int, u8, C.c_int, C.POINTER(C.c_int), u64]),
        ("far", None, [board, C.c_uint8, C.c_uint8]),
        ("rise", None, [board, C.c_uint8, C.c_int]),
        ("run", None, [board, u64]),
        ("get", None, [board, C.POINTER(u64)]),
        ("clear", None, [board]),
        ("log", None, [board, C.c_int]),
        ("records", C.c_size_t, [board, C.POINTER(C.c_void_p)]),
        ("save", C.c_size_t, [board, C.POINTER(C.c_void_p)]),
        ("restore", None, [board, u8, C.c_size_t]),
    ):
        function = getattr(lib, "brd_" + name)
        function.restype, function.argtypes = result, args
    return lib


class Board:
    def __init__(self, bus, seed=1):
        self.bus = bus
        self.lib = library()
        self.h = self.lib.brd_new(PULL[bus], seed)
        self.lib.brd_rise(self.h, *RISE[bus])
        self.raw = (C.c_uint64 * 8)()
        self.device = None

    def close(self):
        if self.h:
            self.lib.brd_free(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- what is on the board

    def plug(self, device, delay=None):
        if device.bus != self.bus:
            raise ValueError("%s does not go on a %s board" % (device.name, self.bus))

        def ints(values):
            return None if values is None else (C.c_int32 * len(values))(*values)

        quirks = device.quirks
        code = self.lib.brd_plug(
            self.h, KIND[self.bus], DELAY[self.bus] if delay is None else delay, device.n, bytes(device.out),
            None if device.mask is None else bytes(device.mask), ints(device.next),
            None if device.ack is None else bytes(device.ack), ints(device.a), ints(device.b), ints(device.c),
            ints(device.idle_next), quirks.get("flip_ppm", 0), quirks.get("seed", 1), quirks.get("timeout", 0),
            quirks.get("keeps_state", 0))
        if code:
            raise ValueError("%s does not plug in" % device.name)
        self.device = device

    def unplug(self):
        self.lib.brd_plug(self.h, 0, 0, 0, None, None, None, None, None, None, None, None, 0, 1, 0, 0)
        self.device = None

    # ---- the host's side: ui_in and uo_out only

    def load(self, image):
        return self.lib.brd_load(self.h, bytes(image), len(image)) == 0

    def exchange(self, tx, want, limit=None):
        rx, rx2, n2 = C.create_string_buffer(max(want, 1)), C.create_string_buffer(8), C.c_int(0)
        if limit is None:
            limit = 2000000 + 4000 * (len(tx) + want)
        got = self.lib.brd_exchange(self.h, bytes(tx), len(tx), rx, want, rx2, 8, C.byref(n2), limit)
        return (None if got < want else rx.raw[:want]), rx2.raw[:n2.value]

    # ---- the pins' side

    def far(self, mask, value):
        self.lib.brd_far(self.h, mask, value)

    def run(self, clocks):
        self.lib.brd_run(self.h, clocks)

    def pins(self):
        self.lib.brd_get(self.h, self.raw)
        return Pins._make(self.raw)

    @property
    def cycle(self):
        return self.pins().cycle

    def clear(self):
        self.lib.brd_clear(self.h)

    def log(self, on=True):
        self.lib.brd_log(self.h, 1 if on else 0)

    def records(self):
        at = C.c_void_p()
        count = self.lib.brd_records(self.h, C.byref(at))
        return list(RECORD.iter_unpack(C.string_at(at, count * RECORD.size))) if count else []

    def save(self):
        at = C.c_void_p()
        size = self.lib.brd_save(self.h, C.byref(at))
        return C.string_at(at, size)

    def restore(self, state):
        self.lib.brd_restore(self.h, state, len(state))
