"""Record the golden model, cycle by cycle, for the RTL to replay.

    python3 test/lockstep/traces.py --out test/lockstep/build/traces            # the 14 benches
    python3 test/lockstep/traces.py --out DIR --random 300 --cycles 4000         # plus random tests

The golden model is `bakeoff/cand_table/chip.py`, unchanged. A `RecordingChip` is that model with a log:
for every `step` it keeps the pins the model saw and the `(out, oe)` it returned, and for every host call
(`host_can_push`, `host_push`, `host_pop`) the answer and the step it followed. The benches run as they do
in `python3 -m bakeoff.run`, so the RTL is driven by their real stimulus. Random tests drive the model
directly: a random table image, random pins, and random host pushes and pops on every clock.

File format (little-endian), one file per bench or per random batch:
    b"PETR", u32 version (1), u32 traces
    per trace: u16 name length, name, u32 image length, image,
               u32 steps, then per step 3 bytes: pins, out, oe,
               u32 events, then per event 8 bytes: u32 step, u8 kind, u8 channel, u8 value, u8 0,
               u32 first step after which the model's overflow count is not zero (0xFFFFFFFF: never)
    event kinds: 0 host_can_push answered (value 0 or 1), 1 host_push (value is the byte),
                 2 host_pop returned a byte (value), 3 host_pop returned None

It runs on the system Python 3.9 with the standard library only, like the bake-off.
"""
from __future__ import annotations

import argparse
import random
import struct
import sys
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bakeoff.cand_table import entry as E  # noqa: E402
from bakeoff.cand_table.candidate import program  # noqa: E402
from bakeoff.cand_table.chip import TableChip  # noqa: E402
from bakeoff.run import discover_benches  # noqa: E402

NEVER = 0xFFFFFFFF
K_CAN_PUSH, K_PUSH, K_POP, K_POP_NONE = range(4)


class Trace:
    def __init__(self, name: str, image: bytes) -> None:
        self.name = name
        self.image = bytes(image)
        self.steps = bytearray()   # pins, out, oe per step
        self.events: List[Tuple[int, int, int, int]] = []
        self.overflow_step = NEVER

    @property
    def n_steps(self) -> int:
        return len(self.steps) // 3


class RecordingChip(TableChip):
    """The golden model with a log. Each reset and load starts a new trace in `sink`."""

    def __init__(self, sink: List[Trace], name: str) -> None:
        self._sink = sink
        self._name = name
        self._trace: Optional[Trace] = None
        super().__init__()

    def reset(self) -> None:
        super().reset()
        self._trace = None

    def load(self, image: bytes) -> None:
        super().load(image)
        self._trace = Trace("%s#%d" % (self._name, len(self._sink)), image)
        self._sink.append(self._trace)

    def step(self, pins: int) -> Tuple[int, int]:
        out, oe = super().step(pins)
        trace = self._trace
        if trace is None:
            raise RuntimeError("the model stepped before a load")
        trace.steps += bytes((pins & 0xFF, out & 0xFF, oe & 0xFF))
        if self.overflow and trace.overflow_step == NEVER:
            trace.overflow_step = trace.n_steps - 1
        return out, oe

    def _event(self, kind: int, ch: int, value: int) -> None:
        trace = self._trace
        if trace is None or trace.n_steps == 0:
            raise RuntimeError("a host call came before the first step")
        trace.events.append((trace.n_steps - 1, kind, ch, value & 0xFF))

    def host_can_push(self, ch: int) -> bool:
        answer = super().host_can_push(ch)
        self._event(K_CAN_PUSH, ch, 1 if answer else 0)
        return answer

    def host_push(self, ch: int, byte: int) -> None:
        super().host_push(ch, byte)
        self._event(K_PUSH, ch, byte)

    def host_pop(self, ch: int) -> Optional[int]:
        byte = super().host_pop(ch)
        if ch in (1, 2):
            self._event(K_POP if byte is not None else K_POP_NONE, ch, byte or 0)
        elif byte is not None:  # the model never returns a byte here; the RTL has no such port
            raise RuntimeError("host_pop(%d) returned a byte" % ch)
        return byte


def write_traces(path: Path, traces: List[Trace]) -> None:
    out = bytearray(b"PETR")
    out += struct.pack("<II", 1, len(traces))
    for t in traces:
        name = t.name.encode()
        out += struct.pack("<H", len(name)) + name
        out += struct.pack("<I", len(t.image)) + t.image
        out += struct.pack("<I", t.n_steps) + t.steps
        out += struct.pack("<I", len(t.events))
        for step, kind, ch, value in t.events:
            out += struct.pack("<IBBBB", step, kind, ch, value, 0)
        out += struct.pack("<I", t.overflow_step)
    path.write_bytes(bytes(out))


# ---- the benches ------------------------------------------------------------------------------------------

def bench_traces(only: Optional[List[str]] = None) -> List[Tuple[str, bool, str, List[Trace]]]:
    """Run every bench on the recording model. Returns (bench, passed, detail, traces) per bench."""
    results = []
    for bench in discover_benches():
        if only and bench.name not in only:
            continue
        image = program(bench.name)
        if image is None:
            continue
        sink: List[Trace] = []
        result = bench.run(lambda: RecordingChip(sink, bench.name), image)
        results.append((bench.name, result.passed, result.detail, sink))
    return results


# ---- random tests -----------------------------------------------------------------------------------------

def random_word(rng: random.Random, mode: str) -> Tuple[int, int]:
    if mode == "uniform":
        return rng.getrandbits(32), rng.getrandbits(32)
    f = E.blank()
    f["base"] = rng.randrange(E.DEPTH) if rng.random() < 0.7 else rng.randrange(16)
    f["ca"] = rng.randrange(16) if rng.random() < 0.7 else 0
    f["cb"] = rng.randrange(16) if rng.random() < 0.4 else 0
    f["p0"], f["p1"] = rng.randrange(4), rng.randrange(4)
    f["p0op"] = rng.randrange(8) if rng.random() < 0.6 else 0
    f["p1op"] = rng.randrange(8) if rng.random() < 0.4 else 0
    f["gate"] = rng.randrange(4)
    f["dsel"] = rng.randrange(2)
    f["din"] = rng.randrange(4)
    roll = rng.random()
    f["imm"] = rng.randrange(6) if roll < 0.4 else rng.getrandbits(16) if roll < 0.8 else rng.randrange(512)
    f["src"] = rng.randrange(8)
    f["dst"] = rng.randrange(8) if rng.random() < 0.6 else 0
    f["shift"] = rng.randrange(2)
    f["crc"] = rng.randrange(8) if rng.random() < 0.6 else 0
    f["nop"] = 3 if rng.random() < 0.08 else rng.randrange(3)
    f["kj"] = rng.randrange(8)
    f["tload"] = 1 if rng.random() < 0.2 else 0
    return E.pack(f)


def random_image(rng: random.Random) -> bytes:
    nm = rng.choice([0, 1, 1, 1, 2, 2, 3, 4, 4])
    configs = []
    for _ in range(nm):
        cfg = E.MachineConfig()
        cfg.out = [rng.randrange(8) for _ in range(4)]
        cfg.inp = [rng.randrange(8) for _ in range(3)]
        cfg.msb = rng.randrange(2)
        cfg.poly = rng.choice([0xEDB88320, 0xA001, 0x8C, rng.getrandbits(32)])
        cfg.init = rng.choice([0, 0xFFFFFFFF, rng.getrandbits(32)])
        cfg.match = rng.choice([0, cfg.init, 0xDEBB20E3, rng.getrandbits(32)])
        configs.append(cfg)
    mode = rng.choice(["uniform", "biased", "biased"])
    count = E.DEPTH if rng.random() < 0.6 else rng.randrange(E.DEPTH + 1)
    words = [random_word(rng, mode) for _ in range(count)]
    return E.build_image(configs, words)


def random_trace(sink: List[Trace], index: int, seed: int, cycles: int) -> None:
    rng = random.Random(seed)
    chip = RecordingChip(sink, "random-%d-seed%d" % (index, seed))
    chip.load(random_image(rng))
    pins = rng.getrandbits(8)
    p_change = rng.choice([0.02, 0.2, 1.0])
    p_push = rng.choice([0.05, 0.3, 0.9])
    p_pop = rng.choice([0.05, 0.3, 0.9])
    for _ in range(cycles):
        if rng.random() < p_change:
            pins ^= rng.getrandbits(8) if p_change == 1.0 else 1 << rng.randrange(8)
        chip.step(pins)
        for ch in (0, 3):
            if rng.random() < p_push and chip.host_can_push(ch):
                chip.host_push(ch, rng.getrandbits(8))
        for ch in (1, 2):
            if rng.random() < p_pop:
                chip.host_pop(ch)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="directory for the .trace files")
    parser.add_argument("--bench", action="append", help="only this bench; repeatable")
    parser.add_argument("--no-benches", action="store_true", help="random tests only")
    parser.add_argument("--random", type=int, default=0, help="number of random tests")
    parser.add_argument("--cycles", type=int, default=4000, help="clocks per random test")
    parser.add_argument("--seed", type=int, default=1, help="first random seed")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for old in args.out.glob("*.trace"):
        old.unlink()

    status = 0
    if not args.no_benches:
        for name, passed, detail, traces in bench_traces(args.bench):
            steps = sum(t.n_steps for t in traces)
            print("recorded %-14s %s  %d traces, %d steps%s" % (
                name, "pass" if passed else "FAIL", len(traces), steps, "" if passed else "  " + detail))
            if not passed:  # the golden model must pass its own bench, or the trace proves nothing
                status = 1
            write_traces(args.out / ("bench_%s.trace" % name), traces)
    batch = 50
    for first in range(0, args.random, batch):
        sink: List[Trace] = []
        for index in range(first, min(first + batch, args.random)):
            random_trace(sink, index, args.seed + index, args.cycles)
        write_traces(args.out / ("random_%04d.trace" % first), sink)
    if args.random:
        print("recorded %d random tests of %d clocks, seeds %d to %d" % (
            args.random, args.cycles, args.seed, args.seed + args.random - 1))
    return status


if __name__ == "__main__":
    sys.exit(main())
