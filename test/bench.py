# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The chip on a board, for the scenario tests. It touches top-level ports only.

Board is the copper round uio[7:0]: pulls, the far end's drivers, rise times on released lines, and a record
of all of it. HostTask is the host's firmware: one thread that loads the program, writes channel 0 and empties
channels 1 and 2 through ui_in and uo_out, as fast as the link allows and no faster.

Times are nanoseconds of a chip at exactly 60 MHz. One simulated clock (host_link.CLOCK_PS) counts as
16.6667 ns, so a limit in ns or ppm means the same here as on the demo board.
"""

from collections import deque

import cocotb
from cocotb.triggers import Event, First, RisingEdge, Timer, ValueChange
from cocotb.utils import get_sim_time

from host_link import (CLOCK_PS, PORT_CH0_CH1, PORT_CH3_CH2, ST_HAS, ST_OVERFLOW, ST_ROOM, ST_RUNNING, Host,
                       program, reset, start_clock)
from protocols.wave import CLK, Wave

PS_PER_NS = CLOCK_PS / CLK
PAD_DELAY = 2.0  # ns from the clock edge to a settled pad, the test's own figure (Tiny Tapeout estimates about
                 # 10 ns for a round trip). It also lets uio_out and uio_oe change together


def now():
    return get_sim_time("ps") / PS_PER_NS


def delay(ns):
    return Timer(max(1, round(ns * PS_PER_NS)), unit="ps")


class Board:
    """The eight protocol pins. `outputs` are the pins the chip may drive at all. `open_drain` are those it may
    only pull low. A pin nobody drives is high, or low if it is in `pulled_low`. `rise` is how long a released
    pin takes to reach high."""

    def __init__(self, dut, outputs, open_drain, pulled_low, rise):
        self.dut = dut
        self.outputs = set(outputs) | set(open_drain)
        self.open_drain, self.rise = set(open_drain), dict(rise)
        self.pull = [0 if pin in pulled_low else 1 for pin in range(8)]
        self.chip_now, self.far_now = [None] * 8, [None] * 8
        self.wire_now, self.line_now = list(self.pull), list(self.pull)
        t = self.since = now()
        self.chip = [Wave(t, None) for _ in range(8)]  # what the chip drives
        self.far = [Wave(t, None) for _ in range(8)]  # what the far end drives
        self.wire = [Wave(t, level) for level in self.pull]  # the line as released or driven, with no rise time
        self.line = [Wave(t, level) for level in self.pull]  # the line as everyone sees it
        self.faults = []
        self.listeners, self.news, self.telling = [], deque(), False
        self.rising = [0] * 8
        cocotb.start_soon(self.watch_chip())

    def listen(self, callback):
        """callback(t, {pin: level}) runs on every change of a line."""
        self.listeners.append(callback)

    def level(self, pin):
        return self.line_now[pin]

    def fault(self, text):
        if len(self.faults) < 5:
            self.faults.append("%s at %.0f ns" % (text, now()))

    async def watch_chip(self):
        self.read_chip()
        while True:
            await First(ValueChange(self.dut.uio_out), ValueChange(self.dut.uio_oe))
            await delay(PAD_DELAY)
            self.read_chip()

    def read_chip(self):
        oe, out = str(self.dut.uio_oe.value), str(self.dut.uio_out.value)  # pin 7 first
        pins = []
        for pin in range(8):
            enable, value = oe[7 - pin], out[7 - pin]
            level = None
            if enable != "0":
                if enable != "1" or value not in "01":
                    self.fault("P%d is X" % pin)
                else:
                    level = int(value)
            if level == self.chip_now[pin]:
                continue
            if level is not None and pin not in self.outputs:
                self.fault("the chip drives P%d, which it must not" % pin)
            if level == 1 and pin in self.open_drain:
                self.fault("the chip drives the open-drain pin P%d high" % pin)
            self.chip_now[pin] = level
            self.chip[pin].set(now(), level)
            pins.append(pin)
        self.settle(pins)

    def drive(self, levels):
        """The far end drives each pin to 0 or 1, or releases it with None."""
        for pin, level in levels.items():
            self.far_now[pin] = level
            self.far[pin].set(now(), level)
        self.settle(levels)

    def settle(self, pins):
        t, changed = now(), {}
        for pin in pins:
            chip, far = self.chip_now[pin], self.far_now[pin]
            if chip is not None and far is not None and chip != far:
                self.fault("contention on P%d" % pin)
            released = chip is None and far is None
            wire = self.pull[pin] if released else (0 if 0 in (chip, far) else 1)
            if wire != self.wire_now[pin]:
                self.wire_now[pin] = wire
                self.wire[pin].set(t, wire)
                self.rising[pin] += 1
                if wire and released and self.rise.get(pin):
                    token = self.rising[pin]
                    self.later(self.rise[pin], lambda p=pin, k=token: self.risen(p, k))
                    continue
            elif wire and self.line_now[pin] == 0 and released:
                continue  # still rising
            if wire != self.line_now[pin]:
                changed[pin] = wire
        self.publish(changed)

    def risen(self, pin, token):
        if token == self.rising[pin] and not self.line_now[pin]:
            self.publish({pin: 1})

    def publish(self, changed):
        if not changed:
            return
        t = now()
        for pin, level in changed.items():
            self.line_now[pin] = level
            self.line[pin].set(t, level)
        self.dut.uio_in.value = sum(level << pin for pin, level in enumerate(self.line_now))
        self.news.append((t, changed))
        if self.telling:
            return  # a listener drove a pin; the others hear of it after the change that caused it
        self.telling = True
        while self.news:
            news = self.news.popleft()
            for listener in self.listeners:
                listener(*news)
        self.telling = False

    def later(self, ns, action):
        async def run():
            await delay(ns)
            action()

        cocotb.start_soon(run())

    def play(self, events):
        """Drive [(t, {pin: level})] at those times."""
        merged = {}
        for t, levels in events:
            merged.setdefault(t, {}).update(levels)

        async def run():
            for t in sorted(merged):
                if t > now():
                    await delay(t - now())
                self.drive(merged[t])

        return cocotb.start_soon(run())


class HostTask:
    """The host's one thread. It serves port 0 (write channel 0, read channel 1) and looks at port 1 (read
    channel 2) every `poll` ns."""

    def __init__(self, dut, host, poll):
        self.dut, self.host, self.poll = dut, host, poll
        self.to_send = deque()
        self.writing = False
        self.received = {1: bytearray(), 2: bytearray()}
        self.kick, self.moved = Event(), Event()
        cocotb.start_soon(self.run())

    def notify(self):
        moved, self.moved = self.moved, Event()
        moved.set()

    async def run(self):
        await RisingEdge(self.dut.clk)
        host, look = self.host, now()
        while True:
            busy = False
            await host.select(PORT_CH0_CH1)
            if self.to_send and host.status() & ST_ROOM:
                self.writing = True
                await host.write(PORT_CH0_CH1, self.to_send.popleft())
                self.writing, busy = False, True
                self.notify()
            if host.status() & ST_HAS:
                self.received[1].append(await host.read(PORT_CH0_CH1))
                busy = True
                self.notify()
            if now() >= look:
                await host.select(PORT_CH3_CH2)
                while host.status() & ST_HAS:
                    self.received[2].append(await host.read(PORT_CH3_CH2))
                    self.notify()
                await host.select(PORT_CH0_CH1)
                look = now() + self.poll
            elif not busy:
                self.kick = Event()
                await First(ValueChange(self.dut.uo_out), delay(look - now()), self.kick.wait())
                await RisingEdge(self.dut.clk)  # the host samples on the clock, like the RP2350's PIO


class Bench:
    def __init__(self, dut, board, host, poll):
        self.dut, self.board, self.host = dut, board, host
        self.ready = now()  # the program runs and has set its idle levels
        self.task = HostTask(dut, host, poll)

    def send(self, data):
        self.task.to_send.extend(data)
        self.task.kick.set()

    def state(self):
        return "ch1 %s, ch2 %s, uo_out %02x, pin faults %s" % (
            self.task.received[1].hex(), self.task.received[2].hex(), self.host.status(), self.board.faults)

    async def wait(self, ns):
        await delay(ns)

    async def until(self, done, what, within):
        deadline = now() + within
        while not done() and now() < deadline:
            await First(self.task.moved.wait(), delay(deadline - now()))
        # The host task wakes us with its pin writes still pending. A test that fails there leaves them to
        # land after Python has shut down, and vvp crashes without writing results.xml. So step past them first.
        await delay(1)
        assert done(), "no %s within %.0f us: %s" % (what, within / 1000, self.state())

    async def sent(self, within):
        await self.until(lambda: not self.task.to_send and not self.task.writing, "room for the host's bytes",
                         within)

    async def expect(self, ch1=b"", ch2=b"", *, within):
        """The host reads exactly these bytes next, on channels 1 and 2."""
        got = self.task.received
        await self.until(lambda: len(got[1]) >= len(ch1) and len(got[2]) >= len(ch2), "answer on the host link",
                         within)
        for channel, want in ((1, ch1), (2, ch2)):
            assert got[channel][:len(want)] == want, "channel %d gave %s, expected %s" % (
                channel, got[channel][:len(want)].hex(), bytes(want).hex())
            del got[channel][:len(want)]

    def driven(self, pins, since=None):
        """The chip drives these push-pull pins from `since` (the end of boot) to now. A pin it lets go reads as
        the board's pull, so the line alone cannot show this."""
        since = self.ready if since is None else since
        for pin in pins:
            wave = self.board.chip[pin]
            released = wave.at(since) is None or any(level is None for _, level in wave.changes(since))
            assert not released, "P%d is left to the board's pull, not driven" % pin

    async def quiet(self, pins, span, within):
        """Wait until the chip has left `pins` alone for `span` ns."""
        deadline = now() + within
        while True:
            t = now()
            await delay(span)
            if not any(self.board.chip[pin].changes(t) for pin in pins):
                return
            assert now() < deadline, "P%s still moving after %.0f us" % (pins, within / 1000)

    async def finish(self):
        """Nothing is left over, nothing was dropped, and no pin rule was broken."""
        await delay(2 * self.task.poll + 100 * CLK)
        assert not self.board.faults, "; ".join(self.board.faults)
        assert not self.task.to_send, "the chip left %d bytes with the host" % len(self.task.to_send)
        for channel, data in self.task.received.items():
            assert not data, "channel %d gave %d bytes too many: %s" % (channel, len(data), data.hex())
        status = self.host.status()
        assert status & ST_RUNNING, "the chip stopped running"
        assert not status & ST_OVERFLOW, "OVERFLOW: a byte was dropped"


async def boot(dut, image, outputs=(), open_drain=(), pulled_low=(), rise=None, far_end=None,
               poll=64 * CLK):
    """Clock, reset and load `image` (a program's name or a compiled image) through the pins. `far_end(board)`
    is plugged in before the load, for a program that starts talking as soon as it runs."""
    start_clock(dut)
    host = Host(dut)
    dut.ena.value = 1
    dut.uio_in.value = 0xFF & ~sum(1 << pin for pin in pulled_low)
    await reset(dut, host)
    board = Board(dut, outputs, open_drain, pulled_low, rise or {})
    if far_end:
        far_end(board)
    await host.load(program(image) if isinstance(image, str) else image)
    await delay(20 * CLK)  # the first states set the idle levels
    return Bench(dut, board, host, poll)
