# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""One run on two chips (tb_pair.v), as learn/lab/replace.py sets it out in a JSON file.

What answers on the bus is the device, the chip loaded with a program through its pins and no device there, or
nothing. What asks is a shipped controller program on a second chip, a query engine on that chip, or the
repository's own SPI-target tester (test/protocols/spi.py). The run judges nothing but the rules of the pins:
it writes what was seen, and replace.py compares."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import First, ReadOnly, Timer, ValueChange

TEST = str(Path(__file__).resolve().parents[3] / "test")
if TEST not in sys.path:
    sys.path.insert(0, TEST)  # the pin tests' bench, host and protocol judges, used as they are

from bench import HostTask, delay, now  # noqa: E402
from host_link import ST_OVERFLOW, Host, program, reset  # noqa: E402
from protocols import i2c, spi  # noqa: E402
from protocols.wave import CLK, Wave  # noqa: E402

from learn.lab import devices  # noqa: E402
from learn.lab.pair.shells import SHELL  # noqa: E402

PARTS = {cls.name: cls for cls in (devices.Flash, devices.Sensor, devices.Thermometer)}
TARGET_PIN = {"spi": 2, "i2c": 1}
WIRING = {"spi": (0x1C, 0x00), "i2c": (0x13, 0x03)}  # pull-ups, open drain
FAILED = -1


class Lines:
    """The eight pins as they were through the whole run, and the far end's drivers."""

    def __init__(self, dut):
        self.dut = dut
        self.far_oe = self.far_out = 0
        self.listeners, self.tasks, self.faults = [], [], []
        t = now()
        names = ("line", "wire", "chip", "ctl", "far")
        for name in names:
            setattr(self, name, [Wave(t, None) for _ in range(8)])
        self.level = [0] * 8
        self.ready = False

    def listen(self, callback):
        self.listeners.append(callback)

    def soon(self, coroutine):
        cocotb.start_soon(coroutine)

    def drive(self, pin, level):
        bit = 1 << pin
        self.far_oe = self.far_oe | bit if level is not None else self.far_oe & ~bit
        self.far_out = self.far_out | bit if level else self.far_out & ~bit
        self.dut.far_out.value = self.far_out
        self.dut.far_oe.value = self.far_oe

    def fault(self, text):
        if len(self.faults) < 5:
            self.faults.append("%s at %.0f ns" % (text, now()))

    def read(self):
        dut, t = self.dut, now()
        texts = [str(sig.value) for sig in (dut.seen, dut.wired, dut.x_oe, dut.x_out, dut.c_oe, dut.c_out,
                                            dut.far_oe, dut.far_out)]
        changed = {}
        for pin in range(8):
            seen, wired, xe, xo, ce, co, fe, fo = [text[7 - pin] for text in texts]
            drivers = []
            for enable, out, waves in ((xe, xo, self.chip), (ce, co, self.ctl), (fe, fo, self.far)):
                level = None
                if enable != "0":
                    if enable != "1" or out not in "01":
                        if self.ready:
                            self.fault("P%d is driven X" % pin)
                    else:
                        level = int(out)
                        drivers.append(level)
                waves[pin].set(t, level)
            if len(set(drivers)) > 1:
                self.fault("contention on P%d" % pin)
            self.wire[pin].set(t, int(wired) if wired in "01" else None)
            if seen not in "01":
                if self.ready:
                    self.fault("P%d reads %s" % (pin, seen))
                continue
            level = int(seen)
            self.line[pin].set(t, level)
            if level != self.level[pin]:
                self.level[pin] = level
                changed[pin] = level
        if changed and self.ready:
            for listener in self.listeners:
                listener(changed, self.level)

    async def watch(self):
        dut = self.dut
        while True:
            await ReadOnly()
            self.read()
            await First(*[ValueChange(sig) for sig in (dut.seen, dut.wired, dut.x_oe, dut.x_out, dut.c_oe,
                                                       dut.c_out, dut.far_oe, dut.far_out)])


def spans(values):
    return [round(min(values), 1), round(max(values), 1), len(values)] if values else None


def delays(lines, bus, waves, since):
    """How long after the edge that causes it the target changes its pin: [least, most, how many] by edge."""
    found = {}
    if bus == "spi":
        causes = [(t, "SCK falls") for t, level in lines.line[0].changes(since) if not level]
        causes += [(t, "CS rises" if level else "CS falls") for t, level in lines.line[3].changes(since)]
    else:
        causes = [(t, "SCL falls") for t, level in lines.line[0].changes(since) if not level]
    causes.sort()
    at = 0
    last = None
    for t, _ in waves[TARGET_PIN[bus]].changes(since):
        while at < len(causes) and causes[at][0] <= t:
            last = causes[at]
            at += 1
        if last:
            found.setdefault(last[1], []).append(t - last[0])
    return {edge: spans(values) for edge, values in found.items()}


async def shipped(cfg, host, lines, out):
    """Each request in turn, and what the controller's host read for it."""
    bus, task = cfg["bus"], host.task
    for request in cfg["requests"]:
        data = bytes(request)
        task.to_send.extend(data)
        task.kick.set()
        if bus == "spi":
            wanted = (data[0], 0)
        else:
            wanted = (0, 1)
        deadline = now() + 3e6
        got = task.received
        while now() < deadline and (len(got[1]) < wanted[0] or len(got[2]) < wanted[1] or task.to_send
                                    or task.writing):
            await First(task.moved.wait(), delay(2000))
        answer = [list(got[1]), list(got[2])]
        del got[1][:], got[2][:]
        out["answers"].append(answer if now() < deadline else FAILED)
        if now() >= deadline:
            return


async def queries(cfg, host, lines, out):
    """Queries as the learner put them to its query engine, and what came back."""
    task = host.task
    for data, wanted in cfg["queries"]:
        task.to_send.extend(bytes.fromhex(data))
        task.kick.set()
        deadline = now() + 30e6
        while now() < deadline and (len(task.received[1]) < wanted or task.to_send or task.writing):
            await First(task.moved.wait(), delay(2000))
        out["answers"].append(task.received[1].hex())
        del task.received[1][:]


async def target_tester(cfg, lines, out):
    """test/protocols/spi.py's controller at 2 MHz, and its check of a target."""
    pins = (0, 1, 2, 3)
    for pin, level in ((0, 0), (1, 0), (3, 1)):
        lines.drive(pin, level)
    await delay(2000)
    for data in cfg["tester"]:
        idle = now()
        events, rises, end = spi.controller_mode0(pins, bytes(data), idle + 2000)
        for at, levels in sorted(events, key=lambda event: event[0]):
            if at > now():
                await delay(at - now())
            for pin, level in levels.items():
                lines.drive(pin, level)
        await delay(1000)
        driven = lines.chip[2] if cfg["answers_with"] != "device" else lines.far[2]
        try:
            sent = spi.check_target(lines.line[2], driven, idle + 2000, rises, end, idle + 1)
            out["answers"].append(list(sent))
        except AssertionError as failure:
            out["answers"].append(str(failure).splitlines()[0])


class Port:
    """One of the two chips, as test/host_link.py's host sees a chip."""

    def __init__(self, dut, letter):
        self.dut = SimpleNamespace(**{name: getattr(dut, "%s_%s" % (letter, short)) for name, short in (
            ("clk", "clk"), ("rst_n", "rst_n"), ("ui_in", "ui"), ("uo_out", "uo"), ("uio_oe", "oe"))})
        self.host = Host(self.dut)
        self.task = None

    async def start(self, image, poll):
        await reset(self.dut, self.host)
        await self.host.load(image)
        self.task = HostTask(self.dut, self.host, poll)


@cocotb.test()
async def one_run(dut):
    cfg = json.loads(Path(os.environ["LEARN_PAIR"]).read_text())
    bus, answers_with = cfg["bus"], cfg["answers_with"]
    dut.c_rst_n.value = 0
    dut.x_rst_n.value = 0
    for name in ("c_ui", "x_ui", "far_oe", "far_out"):
        getattr(dut, name).value = 0
    dut.pull.value, dut.od.value = WIRING[bus]
    cocotb.start_soon(Clock(dut.x_clk, 16666, unit="ps").start())
    await Timer(3001, unit="ps")  # the two chips' clocks are strangers
    cocotb.start_soon(Clock(dut.c_clk, cfg["clock_ps"], unit="ps").start())
    await delay(1000)
    lines = Lines(dut)
    cocotb.start_soon(lines.watch())
    await delay(1000)
    asking, answering = Port(dut, "c"), Port(dut, "x")
    if answers_with == "chip":
        await answering.start(bytes.fromhex(cfg["image"]), 50000)
    await delay(5000)
    lines.ready = True
    if answers_with == "device":
        SHELL[bus](lines, PARTS[cfg["part"]]())

    out = {"answers": []}
    since = now()
    if cfg.get("tester"):
        await target_tester(cfg, lines, out)
    else:
        image = program(cfg["program"]) if cfg.get("program") else bytes.fromhex(cfg["engine"])
        await asking.start(image, 64 * CLK)
        await delay(5000)
        since = now()
        await (queries if cfg.get("queries") else shipped)(cfg, asking, lines, out)
    await delay(50000 if answers_with == "chip" else 5000)

    pin = TARGET_PIN[bus]
    waves = lines.chip if answers_with == "chip" else lines.far
    for other in range(8):
        if lines.chip[other].changes(since) and (answers_with != "chip" or other != pin):
            lines.fault("the chip under test drives P%d, which it must not" % other)
        if bus == "i2c" and other == pin and any(v for _, v in lines.chip[other].changes(since)):
            lines.fault("the chip under test drives SDA high")
        if answers_with != "device" and not cfg.get("tester") and lines.far[other].changes(0):
            lines.fault("something other than the two chips drove P%d" % other)
    if answers_with == "chip":
        if answering.task.received[1] or answering.task.received[2]:
            lines.fault("the chip under test sent its host %s" % dict(answering.task.received))
        if int(dut.x_uo.value) & ST_OVERFLOW:
            lines.fault("OVERFLOW on the chip under test")
    out["bus_check"] = None
    if bus == "i2c" and cfg.get("program"):
        try:
            board = SimpleNamespace(line=lines.line, wire=lines.wire, chip=waves, far=lines.ctl)
            out["decoded"] = i2c.check(board, 0, 1, i2c.FAST, since)[0]
        except AssertionError as failure:
            out["bus_check"] = str(failure).splitlines()[0]
    out["delays"] = delays(lines, bus, waves, since)
    out["faults"] = lines.faults
    out["simulated_us"] = round(now() / 1000)
    Path(cfg["result"]).write_text(json.dumps(out))
