"""Hand-written behavioural chips for the self-test. They are plain Python, not an architecture.

Each fake does one scenario correctly, and takes keyword options that plant one fault each. The self-test
(`selftest.py`) checks that every bench passes the correct fake and fails every faulty one.

A fake is written as straight-line code: `Script.tasks` returns generators that each `yield` once per
clock. Between two yields a task sees `self.pins` as the chip would on that clock (two cycles old), and
what it leaves in `self.out` and `self.oe` reaches the pads one cycle later.
"""
from __future__ import annotations

import random
from collections import deque
from typing import Deque, Dict, Generator, List, Optional, Tuple

from .chip import CH_AUX, CH_RX, CH_STATUS, CH_TX, Chip
from .codec import (
    USB_CRC16_RESIDUAL,
    USB_SYNC_BITS,
    crc8_onewire,
    crc16_usb,
    crc32_eth,
    manchester_encode,
    crc16_usb_register,
    nrzi_decode,
    nrzi_encode,
    usb_stuff,
    usb_unstuff,
)

Task = Generator[None, None, None]


class Script(Chip):
    """A fake chip whose behaviour is one or more generators stepped once per clock."""

    name = "fake"

    def __init__(self, **options: object) -> None:
        self.options = options
        self.reset()

    def opt(self, key: str, default: object = None) -> object:
        return self.options.get(key, default)

    def reset(self) -> None:
        self.now = 0  # how many clocks have gone by
        self.pins = 0xFF
        self.out = 0
        self.oe = 0
        self.inbox: Dict[int, Deque[int]] = {CH_TX: deque(), CH_AUX: deque()}
        self.outbox: Dict[int, Deque[int]] = {CH_RX: deque(), CH_STATUS: deque()}
        self._tasks: List[Task] = list(self.tasks())

    def load(self, image: bytes) -> None:
        pass

    def tasks(self) -> List[Task]:
        return [self.main()]

    def main(self) -> Task:
        while True:
            yield

    def step(self, pins: int) -> Tuple[int, int]:
        self.pins = pins
        finished = None
        for task in self._tasks:
            try:
                next(task)
            except StopIteration:
                finished = task
        if finished is not None:
            self._tasks.remove(finished)
        self.now += 1
        return self.out, self.oe

    def host_can_push(self, ch: int) -> bool:
        return ch in self.inbox

    def host_push(self, ch: int, byte: int) -> None:
        self.inbox[ch].append(byte)

    def host_pop(self, ch: int) -> Optional[int]:
        box = self.outbox.get(ch)
        return box.popleft() if box else None

    def budget(self) -> Dict[str, object]:
        return {}

    # What a task may do between two yields.

    def pin(self, n: int) -> int:
        return (self.pins >> n) & 1

    def drive(self, n: int, level: int) -> None:
        self.oe |= 1 << n
        self.out = (self.out | (1 << n)) if level else (self.out & ~(1 << n))

    def float(self, n: int) -> None:
        self.oe &= ~(1 << n)

    def open_drain(self, n: int, level: int) -> None:
        """Pull pin `n` low, or let go of it for a 1."""
        if level:
            self.float(n)
        else:
            self.drive(n, 0)

    def push(self, ch: int, byte: int) -> None:
        self.outbox[ch].append(byte & 0xFF)

    def wait(self, n: float) -> Task:
        for _ in range(int(n)):
            yield

    def until(self, time: float) -> Task:
        while self.now < time:
            yield

    def take(self, ch: int = CH_TX) -> Generator[None, None, int]:
        """Wait for a byte from the host: `byte = yield from self.take()`."""
        while not self.inbox[ch]:
            yield
        return self.inbox[ch].popleft()

    def play(self, pin_levels: List[Tuple[int, int]]) -> Task:
        """Drive `(out, oe)` pairs, one per clock."""
        for out, oe in pin_levels:
            self.out, self.oe = out, oe
            yield


class Stuck(Script):
    """Drives every pin to one level for ever, and never touches the host link. Option: `level` (0 or 1)."""

    name = "stuck"

    def main(self) -> Task:
        self.out = 0xFF if self.opt("level") else 0x00
        self.oe = 0xFF
        while True:
            yield


class Noise(Chip):
    """Flips a random pin's level or direction now and then, and pushes random bytes to the host."""

    name = "noise"

    def __init__(self, seed: int = 1, rate: float = 0.05) -> None:
        self.seed = seed
        self.rate = rate
        self.reset()

    def reset(self) -> None:
        self.rng = random.Random(self.seed)
        self.out = 0
        self.oe = 0

    def load(self, image: bytes) -> None:
        pass

    def step(self, pins: int) -> Tuple[int, int]:
        if self.rng.random() < self.rate:
            bit = 1 << self.rng.randrange(8)
            if self.rng.random() < 0.5:
                self.out ^= bit
            else:
                self.oe ^= bit
        return self.out, self.oe

    def host_can_push(self, ch: int) -> bool:
        return True

    def host_push(self, ch: int, byte: int) -> None:
        pass

    def host_pop(self, ch: int) -> Optional[int]:
        return self.rng.randrange(256) if ch in (CH_RX, CH_STATUS) and self.rng.random() < 0.02 else None

    def budget(self) -> Dict[str, object]:
        return {}


class FakeUart(Script):
    """8N1 on P0 (TX) and P1 (RX), both at once.

    Options: `tx_bit` and `rx_bit` (bit times in cycles), `tx_msb_first`, `rx_msb_first`, `full_stop` (the
    receiver waits out a whole stop bit before it looks for the next start), `half_duplex` (the receiver
    is deaf while the transmitter is busy) and `drive_rx` (the chip drives its own RX pin low).
    """

    name = "fake_uart"
    TX, RX = 0, 1

    def tasks(self) -> List[Task]:
        self.sending = False
        return [self.transmit(), self.receive()]

    def transmit(self) -> Task:
        bit = float(self.opt("tx_bit", 20))
        self.drive(self.TX, 1)
        if self.opt("drive_rx"):
            self.drive(self.RX, 0)
        while True:
            byte = yield from self.take()
            self.sending = True
            order = range(7, -1, -1) if self.opt("tx_msb_first") else range(8)
            start = self.now
            for k, level in enumerate([0] + [(byte >> b) & 1 for b in order] + [1]):
                self.drive(self.TX, level)
                yield from self.until(start + (k + 1) * bit)
            self.sending = False

    def receive(self) -> Task:
        bit = float(self.opt("rx_bit", 20))
        while True:
            while self.pin(self.RX) or (self.opt("half_duplex") and self.sending):
                yield
            start = self.now
            byte = 0
            for k in range(8):
                yield from self.until(start + (1.5 + k) * bit)
                byte |= self.pin(self.RX) << ((7 - k) if self.opt("rx_msb_first") else k)
            yield from self.until(start + (10.0 if self.opt("full_stop") else 9.5) * bit)
            if self.pin(self.RX) or self.opt("full_stop"):
                self.push(CH_RX, byte)
            while not self.pin(self.RX):  # a framing error: wait for the line to go idle
                yield


class FakeSpiController(Script):
    """An SPI controller on P0 (SCK), P1 (MOSI), P2 (MISO), P3 (CS).

    Options: `mode` (0 or 3), `half` (half an SCK period, in cycles), `lead` (cycles from CS falling to
    the first rising edge), `lsb_first`, `sample_delay` (clocks between raising SCK and reading MISO),
    `keep_cs` (CS is never raised), `mosi_on_rise` (MOSI changes together with the rising edge),
    `byte_gap` (idle clocks after each byte) and `start_delay` (clocks of doing nothing after reset).
    """

    name = "fake_spi_controller"
    SCK, MOSI, MISO, CS = 0, 1, 2, 3

    def tasks(self) -> List[Task]:
        self.seen: List[int] = []
        return [self.watch(), self.main()]

    def watch(self) -> Task:
        while True:
            self.seen.append(self.pins)
            yield

    def main(self) -> Task:
        mode = int(self.opt("mode", 0))
        half = int(self.opt("half", 6))
        lead = int(self.opt("lead", half))
        self.drive(self.SCK, 1 if mode == 3 else 0)
        self.drive(self.CS, 1)
        self.drive(self.MOSI, 0)
        yield from self.wait(int(self.opt("start_delay", 0)))
        n = yield from self.take()
        data = []
        for _ in range(n):
            data.append((yield from self.take()))
        order = range(8) if self.opt("lsb_first") else range(7, -1, -1)
        bits = [(byte >> b) & 1 for byte in data for b in order]
        self.drive(self.CS, 0)
        if lead > half:
            if mode == 0:
                self.drive(self.MOSI, bits[0])
            yield from self.wait(lead - half)
        sample_at = []
        for k, bit in enumerate(bits):
            if mode == 3:
                self.drive(self.SCK, 0)
            if not (self.opt("mosi_on_rise") and k):
                self.drive(self.MOSI, bit)
            yield from self.wait(min(lead, half) if k == 0 else half)
            self.drive(self.SCK, 1)
            if self.opt("mosi_on_rise") and k + 1 < len(bits):
                self.drive(self.MOSI, bits[k + 1])
            sample_at.append(self.now + int(self.opt("sample_delay", 0)))
            yield from self.wait(half)
            if mode == 0:
                self.drive(self.SCK, 0)
            if k % 8 == 7:
                yield from self.wait(int(self.opt("byte_gap", 0)))
        if not self.opt("keep_cs"):
            self.drive(self.CS, 1)
        yield from self.until(sample_at[-1] + 1)
        read = [(self.seen[t] >> self.MISO) & 1 for t in sample_at]
        for i in range(0, len(read), 8):
            self.push(CH_RX, sum(bit << (7 - j) for j, bit in enumerate(read[i : i + 8])))


class FakeSpiTarget(Script):
    """An SPI mode 0 target on P0 (SCK), P1 (MOSI), P2 (MISO), P3 (CS).

    Options: `lag` (extra clocks before MISO follows a falling edge), `always_drive` (MISO is driven from
    reset), `hold` (MISO stays driven after CS rises), `lsb_first` and `no_push`.
    """

    name = "fake_spi_target"
    SCK, MOSI, MISO, CS = 0, 1, 2, 3

    def main(self) -> Task:
        data = []
        for _ in range(8):
            data.append((yield from self.take()))
        order = range(8) if self.opt("lsb_first") else range(7, -1, -1)
        bits = [(byte >> b) & 1 for byte in data for b in order]
        if self.opt("always_drive"):
            self.drive(self.MISO, 1)
        while self.pin(self.CS):
            yield
        index = 0
        received: List[int] = []
        self.drive(self.MISO, bits[0])
        last = self.pin(self.SCK)
        while not self.pin(self.CS):
            sck = self.pin(self.SCK)
            if sck and not last:
                received.append(self.pin(self.MOSI))
            if last and not sck:
                index += 1
                yield from self.wait(int(self.opt("lag", 0)))
                self.drive(self.MISO, bits[index] if index < len(bits) else 1)
            last = sck
            yield
        if not self.opt("hold"):
            self.float(self.MISO)
        if not self.opt("no_push"):
            for i in range(0, len(received) - 7, 8):
                self.push(CH_RX, sum(bit << (7 - j) for j, bit in enumerate(received[i : i + 8])))


class FakeI2cController(Script):
    """An I2C controller on P0 (SCL) and P1 (SDA), bit-banged with open-drain pins.

    Options: `low` and `high` (how long it holds each SCL level, in clocks; the pads add 3 to the high
    time), `setup` (clocks between changing SDA and releasing SCL), `no_wait` (it does not wait for a
    stretched SCL to rise), `push_pull` (it drives SCL high), `ack_last` (it ACKs the last byte it reads),
    `ignore_nack`, `always_ok` (the status is 0x00 whatever happened), `no_status`, `sda_early` (SDA
    changes just before SCL falls) and `stop_start` (a STOP and a START where the repeated START belongs).
    """

    name = "fake_i2c_controller"
    SCL, SDA = 0, 1

    def scl(self, level: int) -> None:
        if level and self.opt("push_pull"):
            self.drive(self.SCL, 1)
        else:
            self.open_drain(self.SCL, level)

    def scl_high(self) -> Task:
        """Release SCL, wait for it to rise (a target may hold it), and stay high for the high time."""
        self.scl(1)
        if self.opt("no_wait"):
            yield from self.wait(3)
        else:
            yield
            while not self.pin(self.SCL):
                yield
        yield from self.wait(int(self.opt("high", 50)))

    def clock_bit(self, level: int) -> Generator[None, None, int]:
        """With SCL low: put `level` on SDA, give one clock pulse and return SDA as it was while SCL was high."""
        low = int(self.opt("low", 100))
        setup = int(self.opt("setup", low - 4))
        yield from self.wait(low - setup)
        self.open_drain(self.SDA, level)
        yield from self.wait(setup)
        yield from self.scl_high()
        bit = self.pin(self.SDA)
        if self.opt("sda_early"):
            self.open_drain(self.SDA, 1 - level)
            yield from self.wait(3)
        self.scl(0)
        return bit

    def start(self) -> Task:
        """From idle, or from SCL low for a repeated START."""
        if self.oe & (1 << self.SCL) and not self.out & (1 << self.SCL):
            yield from self.wait(4)
            self.open_drain(self.SDA, 1)
            yield from self.wait(int(self.opt("low", 100)) - 4)
            yield from self.scl_high()
        self.open_drain(self.SDA, 0)
        yield from self.wait(40)
        self.scl(0)

    def stop(self) -> Task:
        yield from self.wait(4)
        self.open_drain(self.SDA, 0)
        yield from self.wait(int(self.opt("low", 100)) - 4)
        yield from self.scl_high()
        self.open_drain(self.SDA, 1)
        self.open_drain(self.SCL, 1)

    def write(self, byte: int) -> Generator[None, None, bool]:
        for b in range(7, -1, -1):
            yield from self.clock_bit((byte >> b) & 1)
        nack = yield from self.clock_bit(1)
        return not nack or bool(self.opt("ignore_nack"))

    def read(self, ack: bool) -> Generator[None, None, int]:
        byte = 0
        for _ in range(8):
            byte = (byte << 1) | (yield from self.clock_bit(1))
        yield from self.clock_bit(0 if ack else 1)
        return byte

    def main(self) -> Task:
        address = yield from self.take()
        to_write = []
        for _ in range((yield from self.take())):
            to_write.append((yield from self.take()))
        n_read = yield from self.take()
        yield from self.start()
        ok = yield from self.write(address << 1)
        for byte in to_write:
            ok = ok and (yield from self.write(byte))
        data = []
        if ok:
            if self.opt("stop_start"):
                yield from self.stop()
                yield from self.wait(200)
            yield from self.start()
            ok = yield from self.write((address << 1) | 1)
        if ok:
            for i in range(n_read):
                data.append((yield from self.read(i + 1 < n_read or bool(self.opt("ack_last")))))
        yield from self.stop()
        for byte in data:
            self.push(CH_RX, byte)
        if not self.opt("no_status"):
            self.push(CH_STATUS, 0 if ok or self.opt("always_ok") else 1)


class FakeWs2812(Script):
    """A WS2812 sender on P0.

    Options: `t0h`, `t0l`, `t1h`, `t1l` (the four times, in cycles), `lsb_first`, `byte_gap` (extra low
    cycles after each byte), `idle_high` (the line rests high) and `encore` (one more pulse this many cycles
    after the last bit).
    """

    name = "fake_ws2812"
    PIN = 0

    def main(self) -> Task:
        idle = 1 if self.opt("idle_high") else 0
        self.drive(self.PIN, idle)
        n = yield from self.take()
        data = []
        for _ in range(3 * n):
            data.append((yield from self.take()))
        order = range(8) if self.opt("lsb_first") else range(7, -1, -1)
        for byte in data:
            for b in order:
                bit = (byte >> b) & 1
                self.drive(self.PIN, 1)
                yield from self.wait(int(self.opt("t1h", 48) if bit else self.opt("t0h", 24)))
                self.drive(self.PIN, 0)
                yield from self.wait(int(self.opt("t1l", 27) if bit else self.opt("t0l", 51)))
            yield from self.wait(int(self.opt("byte_gap", 0)))
        self.drive(self.PIN, idle)
        if self.opt("encore"):
            yield from self.wait(int(self.opt("encore")))
            self.drive(self.PIN, 1)
            yield from self.wait(24)
            self.drive(self.PIN, 0)


class FakePs2(Script):
    """A PS/2 listener on P0 (CLK) and P1 (DATA).

    Options: `msb_first`, `no_parity_check` (every byte goes to channel 1 and nothing to channel 2),
    `keep_bad` (the bad byte is flagged and also pushed to channel 1), `rising` (DATA is sampled on the
    rising clock edge) and `pull_data` (the chip pulls DATA low during the first frame).
    """

    name = "fake_ps2"
    CLK, DATA = 0, 1

    def main(self) -> Task:
        edge = 1 if self.opt("rising") else 0
        last = 1
        bits: List[int] = []
        if self.opt("pull_data"):
            self.drive(self.DATA, 0)
        while True:
            clk = self.pin(self.CLK)
            if clk != last and clk == edge:
                bits.append(self.pin(self.DATA))
            last = clk
            if len(bits) == 11:
                data = bits[1:9]
                byte = sum(bit << ((7 - k) if self.opt("msb_first") else k) for k, bit in enumerate(data))
                good = bits[0] == 0 and bits[10] == 1 and (sum(data) + bits[9]) % 2 == 1
                if good or self.opt("no_parity_check") or self.opt("keep_bad"):
                    self.push(CH_RX, byte)
                if not good and not self.opt("no_parity_check"):
                    self.push(CH_STATUS, 0xFF)
                bits = []
            yield


class FakeOneWire(Script):
    """A 1-Wire controller on P0. All times are in cycles (60 per microsecond).

    Options: `reset` (the reset low time), `one` and `zero` (the low times of a written 1 and 0), `slot`
    (the spacing of slot starts), `sample` (when a read slot is sampled, from its start), `command`,
    `msb_first`, `push_pull` (the chip drives DQ high to end a low time), `no_crc` (the CRC is always
    reported good), `ignore_absent` (it carries on when nobody answers the reset) and `no_presence_byte`.
    """

    name = "fake_onewire"
    DQ = 0

    def low(self, cycles: int) -> Task:
        self.drive(self.DQ, 0)
        yield from self.wait(cycles)
        if self.opt("push_pull"):
            self.drive(self.DQ, 1)
            yield
        self.float(self.DQ)

    def main(self) -> Task:
        slot = int(self.opt("slot", 4200))
        yield from self.wait(100)
        yield from self.low(int(self.opt("reset", 30000)))
        yield from self.wait(4200)  # 70 microseconds after the release: the presence pulse is on
        present = not self.pin(self.DQ)
        yield from self.wait(24600)
        if not present and not self.opt("ignore_absent"):
            self.push(CH_STATUS, 1)
            return
        command = int(self.opt("command", 0x33))
        order = range(7, -1, -1) if self.opt("msb_first") else range(8)
        for b in order:
            start = self.now
            yield from self.low(int(self.opt("one", 360) if (command >> b) & 1 else self.opt("zero", 3900)))
            yield from self.until(start + slot)
        bits: List[int] = []
        for _ in range(64):
            start = self.now
            yield from self.low(int(self.opt("one", 360)))
            yield from self.until(start + int(self.opt("sample", 780)))
            bits.append(self.pin(self.DQ))
            yield from self.until(start + slot)
        rom = bytes(sum(bit << k for k, bit in enumerate(bits[i : i + 8])) for i in range(0, 64, 8))
        for byte in rom:
            self.push(CH_RX, byte)
        if not self.opt("no_presence_byte"):
            self.push(CH_STATUS, 0)
        self.push(CH_STATUS, 0 if crc8_onewire(rom) == 0 or self.opt("no_crc") else 1)


class FakeUsbTx(Script):
    """A low-speed USB sender on P0 (D+) and P1 (D-).

    Options: `bit` (the bit time in cycles), `no_stuff`, `bad_crc`, `crc_high_first`, `msb_first`,
    `bare_handshake` (a packet with no data gets no CRC), `se0_bits` (how long SE0 lasts), `no_j` (it lets
    go straight after SE0), `keep_driving` (it never lets go), `late_edge` (one edge comes 4 cycles late)
    and `skew` (D- follows D+ by this many cycles).
    """

    name = "fake_usb_tx"
    DP, DM = 0, 1

    def state(self, level: int) -> None:
        self.drive(self.DP, 0 if level else 1)
        self.drive(self.DM, 1 if level else 0)

    def main(self) -> Task:
        bit = float(self.opt("bit", 40))
        n = yield from self.take()
        body = []
        for _ in range(n):
            body.append((yield from self.take()))
        crc = crc16_usb(bytes(body[1:])) ^ (0x0100 if self.opt("bad_crc") else 0)
        crc_bytes = [crc >> 8, crc & 0xFF] if self.opt("crc_high_first") else [crc & 0xFF, crc >> 8]
        if not (self.opt("bare_handshake") and n == 1):
            body += crc_bytes
        order = range(7, -1, -1) if self.opt("msb_first") else range(8)
        bits = USB_SYNC_BITS + [(byte >> b) & 1 for byte in body for b in order]
        levels = nrzi_encode(bits if self.opt("no_stuff") else usb_stuff(bits))
        start = self.now
        skew = int(self.opt("skew", 0))
        for k, level in enumerate(levels):
            if self.opt("late_edge") and k == 20:
                yield from self.wait(4)
            if skew and k and level != levels[k - 1]:
                self.drive(self.DP, 0 if level else 1)
                yield from self.wait(skew)
            self.state(level)
            yield from self.until(start + (k + 1) * bit)
        end = start + len(levels) * bit
        self.drive(self.DP, 0)
        self.drive(self.DM, 0)
        yield from self.until(end + float(self.opt("se0_bits", 2)) * bit)
        if not self.opt("no_j"):
            self.state(1)
            yield from self.until(end + (float(self.opt("se0_bits", 2)) + 1) * bit)
        if not self.opt("keep_driving"):
            self.float(self.DP)
            self.float(self.DM)


class FakeUsbRx(Script):
    """A low-speed USB listener on P0 (D+) and P1 (D-). It times the gaps between edges to count bits.

    Options: `free_run` (it samples every 40 cycles from the first edge and never resynchronises),
    `no_unstuff`, `always_good`, `count_data_only` (the byte count leaves out the PID and CRC) and
    `drive` (it drives J all the time).
    """

    name = "fake_usb_rx"
    SE0, K, J = 0, 1, 2

    def main(self) -> Task:
        if self.opt("drive"):
            self.drive(0, 0)
            self.drive(1, 1)
        while True:
            while self.pins & 3 != self.K:
                yield
            levels: List[int] = []
            last, since = self.K, self.now
            while True:
                yield
                state = self.pins & 3
                if self.opt("free_run"):
                    if (self.now - since) % 40 == 20:
                        if state == self.SE0:
                            break
                        levels.append(1 if state == self.J else 0)
                elif state != last and state != 3:
                    levels += [1 if last == self.J else 0] * int(round((self.now - since) / 40.0))
                    if state == self.SE0:
                        break
                    last, since = state, self.now
            bits = nrzi_decode(levels, 1)
            if not self.opt("no_unstuff"):
                bits = usb_unstuff(bits)[0]
            bits = bits[8:]
            body = bytes(sum(bit << k for k, bit in enumerate(bits[i : i + 8])) for i in range(0, len(bits) - 7, 8))
            for byte in body:
                self.push(CH_RX, byte)
            good = crc16_usb_register(body[1:]) == USB_CRC16_RESIDUAL or self.opt("always_good")
            self.push(CH_STATUS, 0 if good else 1)
            self.push(CH_STATUS, len(body) - 3 if self.opt("count_data_only") else len(body))
            while self.pins & 3 != self.J:
                yield


class FakeEthTx(Script):
    """A 10 Mbit Ethernet sender on P0 (TX+) and P1 (TX-). It takes both host messages, then sends.

    Options: `bad_fcs`, `fcs_high_first`, `preamble` (how many 0x55 bytes), `msb_first`, `stretch` (one
    half bit, 200 bits in, lasts 4 cycles), `tail` (how long TX+ stays high at the end), `gap` (idle cycles
    between the frames), `single_ended` (TX- stays low) and `one_frame` (it stops after the first frame).
    """

    name = "fake_eth_tx"
    PLUS, MINUS = 0, 1

    def main(self) -> Task:
        self.drive(self.PLUS, 0)
        self.drive(self.MINUS, 0)
        frames = []
        for _ in range(2):
            length = yield from self.take()
            length |= (yield from self.take()) << 8
            frame = bytearray()
            for _ in range(length):
                frame.append((yield from self.take()))
            frames.append(bytes(frame))
        for index, frame in enumerate(frames):
            fcs = crc32_eth(frame) ^ (1 if self.opt("bad_fcs") else 0)
            wire = b"\x55" * int(self.opt("preamble", 7)) + b"\xd5" + frame
            wire += fcs.to_bytes(4, "big" if self.opt("fcs_high_first") else "little")
            order = range(7, -1, -1) if self.opt("msb_first") else range(8)
            halves = manchester_encode([(byte >> b) & 1 for byte in wire for b in order])
            for k, level in enumerate(halves):
                self.drive(self.PLUS, level)
                self.drive(self.MINUS, 0 if self.opt("single_ended") else 1 - level)
                yield from self.wait(4 if self.opt("stretch") and k == 400 else 3)
            self.drive(self.PLUS, 1)
            self.drive(self.MINUS, 0)
            yield from self.wait(int(self.opt("tail", 18)))
            self.drive(self.PLUS, 0)
            if self.opt("one_frame"):
                return
            yield from self.wait(int(self.opt("gap", 600)))


class FakeEthRx(Script):
    """A 10 Mbit Ethernet listener on P2. It measures the runs between edges, so it follows the sender's clock.

    Options: `free_run` (it samples every 3 cycles from the first edge and never resynchronises),
    `always_good`, `length_without_fcs`, `keep_sfd` (the 0xD5 is pushed too) and `msb_first`.
    """

    name = "fake_eth_rx"
    RX = 2

    def main(self) -> Task:
        while True:
            while not self.pin(self.RX):
                yield
            # The first rising edge is the middle of the first bit (a 1), so its low half was the idle line.
            halves = [0]
            last, since = 1, self.now
            while True:
                yield
                level = self.pin(self.RX)
                run_length = self.now - since
                if self.opt("free_run"):
                    if run_length % 3 == 1:
                        halves.append(level)
                    if halves[-5:] == [1] * 5:
                        del halves[-4:]
                        break
                elif last and run_length > 8:
                    break  # high for longer than a bit: the end of the frame
                elif level != last:
                    halves += [last] * (1 if run_length <= 4 else 2)
                    last, since = level, self.now
            if len(halves) % 2:
                halves.append(1)  # the last bit was a 1, and its high half ran into the end marker
            bits = [halves[i + 1] for i in range(0, len(halves) - 1, 2)]
            sfd = next((i for i in range(1, len(bits)) if bits[i] and bits[i - 1]), len(bits))
            bits = bits[sfd + 1 - (8 if self.opt("keep_sfd") else 0) :]
            order = [(7 - k) if self.opt("msb_first") else k for k in range(8)]
            frame = bytes(
                sum(bit << order[k] for k, bit in enumerate(bits[i : i + 8])) for i in range(0, len(bits) - 7, 8)
            )
            for byte in frame:
                self.push(CH_RX, byte)
            good = len(frame) >= 4 and crc32_eth(frame[:-4]) == int.from_bytes(frame[-4:], "little")
            self.push(CH_STATUS, 0 if good or self.opt("always_good") else 1)
            length = len(frame) - (4 if self.opt("length_without_fcs") else 0)
            self.push(CH_STATUS, length & 0xFF)
            self.push(CH_STATUS, length >> 8)
            while self.pin(self.RX):
                yield
