"""Checks that the benches catch wrong chips, not only that they pass right ones.

    python3 -m bakeoff.harness.selftest

Each bench is run against a hand-written fake chip that does the scenario correctly, and against fakes
with one planted fault each. A bench that passes a faulty fake is itself broken.
"""
from __future__ import annotations

import binascii
import copy
import random
import sys
import types
from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Tuple

from .benches import eth10, i2c, onewire, ps2, spi, uart, usb_ls, ws2812
from .benches.eth10 import Eth10Rx, Eth10Tx
from .benches.i2c import I2cController
from .benches.onewire import OneWire
from .benches.ps2 import Ps2Rx, ps2_frame_bits
from .benches.spi import SpiControllerMode0, SpiControllerMode3, SpiTargetMode0
from .benches.uart import Uart3M, UartRx, UartTx
from .benches.usb_ls import UsbLsRx, UsbLsTx
from .benches.ws2812 import Ws2812Tx
from .chip import CH_TX, CLK_HZ, Chip
from .codec import (
    USB_CRC16_RESIDUAL,
    crc8_onewire,
    crc16_usb,
    crc16_usb_register,
    crc32_eth,
    eth_frame_bytes,
    manchester_encode,
    nrzi_decode,
    nrzi_encode,
    usb_packet_levels,
    usb_stuff,
    usb_unstuff,
)
from .fakes import (
    FakeEthRx,
    FakeEthTx,
    FakeI2cController,
    FakeOneWire,
    FakePs2,
    FakeSpiController,
    FakeSpiTarget,
    FakeUart,
    FakeUsbRx,
    FakeUsbTx,
    FakeWs2812,
    Noise,
    Script,
    Stuck,
)
from .sim import Bench, Sim, edges
from .stim import Stimulus, run

# A case is (label, bench, make_chip, should it pass, text the failure detail must contain).
Case = Tuple[str, Bench, Callable[[], Chip], bool, str]


class FakeUartTx(Chip):
    """Sends every byte from channel 0 as 8N1 on P0. `bit` is the bit time in cycles."""

    name = "fake_uart_tx"

    def __init__(self, bit: float, msb_first: bool = False, stop_level: int = 1) -> None:
        self.bit = bit
        self.msb_first = msb_first
        self.stop_level = stop_level
        self.reset()

    def reset(self) -> None:
        self.fifo: Deque[int] = deque()
        self.frame: List[int] = []
        self.elapsed = 0

    def load(self, image: bytes) -> None:
        pass

    def step(self, pins: int) -> Tuple[int, int]:
        if not self.frame and self.fifo:
            byte = self.fifo.popleft()
            order = range(7, -1, -1) if self.msb_first else range(8)
            self.frame = [0] + [(byte >> b) & 1 for b in order] + [self.stop_level]
            self.elapsed = 0
        level = 1
        if self.frame:
            index = int(self.elapsed / self.bit)
            if index >= len(self.frame):
                self.frame = []
            else:
                level = self.frame[index]
                self.elapsed += 1
        return level, 1

    def host_can_push(self, ch: int) -> bool:
        return ch == CH_TX and len(self.fifo) < 4

    def host_push(self, ch: int, byte: int) -> None:
        self.fifo.append(byte)

    def host_pop(self, ch: int) -> Optional[int]:
        return None

    def budget(self) -> Dict[str, object]:
        return {}


def check_sim_latency() -> List[str]:
    """A chip that copies P1 to P0 must show the copy exactly three cycles later."""

    class Wire(Chip):
        def reset(self) -> None:
            pass

        def load(self, image: bytes) -> None:
            pass

        def step(self, pins: int) -> Tuple[int, int]:
            return (pins >> 1) & 1, 1

        def host_can_push(self, ch: int) -> bool:
            return False

        def host_pop(self, ch: int) -> Optional[int]:
            return None

    sim = Sim(Wire(), b"")
    sim.set(1, 1)
    sim.run(10)
    sim.set(1, 0)
    sim.run(10)
    fall_in = next(i for i in range(1, 20) if sim.wave[i - 1] & 2 and not sim.wave[i] & 2)
    fall_out = next(i for i in range(1, 20) if sim.wave[i - 1] & 1 and not sim.wave[i] & 1)
    return [] if fall_out - fall_in == 3 else ["round trip is %d cycles, expected 3" % (fall_out - fall_in)]


def check_peek() -> List[str]:
    """`Sim.peek` must name the levels the next tick shows, whatever the chip and the bench drive."""

    class Noise(Chip):
        def reset(self) -> None:
            self.rng = random.Random(7)

        def load(self, image: bytes) -> None:
            pass

        def step(self, pins: int) -> Tuple[int, int]:
            return self.rng.randrange(256), self.rng.randrange(256)

        def host_can_push(self, ch: int) -> bool:
            return False

        def host_pop(self, ch: int) -> Optional[int]:
            return None

    rng = random.Random(8)
    sim = Sim(Noise(), b"", pull=0xA5)
    for _ in range(2000):
        pin = rng.randrange(8)
        if rng.random() < 0.3:
            sim.release(pin)
        else:
            sim.set(pin, rng.randrange(2))
        expected = sim.peek()
        if sim.tick() != expected:
            return ["peek and tick disagree at cycle %d" % (sim.cycle - 1)]
    return []


def check_stimulus() -> List[str]:
    """A change scheduled at time t must show from the first whole cycle at or after t."""
    sim = Sim(Script(), b"")
    stim = Stimulus()
    stim.at(0, 0, 0)
    stim.at(10.0, 0, 1)
    stim.at(20.25, 0, 0)
    stim.at(30.99, 0, 1)
    stim.at(40, 0, None)
    stim.at(40, 1, 0)
    run(sim, 50, stim)
    got = [cycle for cycle, _ in edges(sim.wave, 0)]
    problems = [] if got == [0, 10, 21, 31] else ["stimulus edges at %s, expected [0, 10, 21, 31]" % got]
    if edges(sim.wave, 1) != [(0, 1), (40, 0)]:
        problems.append("stimulus on a second pin went wrong: %s" % edges(sim.wave, 1))
    return problems


def check_codecs() -> List[str]:
    """The reference CRCs and line codes against vectors that do not come from this repo."""
    problems: List[str] = []

    def expect(what: str, got: object, want: object) -> None:
        if got != want:
            problems.append("%s is %r, expected %r" % (what, got, want))

    check = b"123456789"
    # The ROM in Maxim's application note 27: family 02, serial 00 00 00 01 B8 1C, CRC A2.
    expect("crc8_onewire of the AN27 ROM", crc8_onewire(bytes.fromhex("021cb801000000")), 0xA2)
    expect("crc8_onewire over a ROM and its CRC", crc8_onewire(bytes.fromhex("021cb801000000a2")), 0)
    expect("crc16_usb check value", crc16_usb(check), 0xB4C8)
    expect("crc16_usb of nothing", crc16_usb(b""), 0x0000)
    # The data packets of two standard requests, as they appear in any bus trace.
    requests = (("Get Descriptor", "8006000100004000", "dd94"), ("Set Address", "0005010000000000", "eb25"))
    for request, data, crc_bytes in requests:
        expect("crc16_usb of %s" % request, crc16_usb(bytes.fromhex(data)).to_bytes(2, "little").hex(), crc_bytes)
    with_crc = check + crc16_usb(check).to_bytes(2, "little")
    expect("crc16_usb residual", crc16_usb_register(with_crc), USB_CRC16_RESIDUAL)
    expect("crc32_eth check value", crc32_eth(check), 0xCBF43926)
    rng = random.Random(3)
    for _ in range(20):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 400)))
        expect("crc32_eth against binascii", crc32_eth(data), binascii.crc32(data))
        expect("crc32_eth residual", crc32_eth(data + crc32_eth(data).to_bytes(4, "little")), 0x2144DF1C)
        bits = [1 if rng.random() < 0.8 else 0 for _ in range(rng.randrange(1, 200))]
        stuffed = usb_stuff(bits)
        expect("unstuff(stuff(bits))", usb_unstuff(stuffed), (bits, None))
        if "1111111" in "".join(map(str, stuffed)):
            problems.append("usb_stuff left seven 1s in a row")
        expect("nrzi_decode(nrzi_encode(bits))", nrzi_decode(nrzi_encode(bits)), bits)
    expect("usb_stuff of eight 1s", usb_stuff([1] * 8), [1, 1, 1, 1, 1, 1, 0, 1, 1])
    expect("usb_unstuff of seven 1s", usb_unstuff([1] * 7)[1], 6)
    expect("SYNC", "".join("J" if level else "K" for level in usb_packet_levels(b"")), "KJKJKJKK")
    # An ACK handshake: SYNC, then 0xD2 LSB first (0 1 0 0 1 0 1 1), NRZI from the K K that ends SYNC.
    expect("an ACK packet", "".join("J" if level else "K" for level in usb_packet_levels(b"\xd2")), "KJKJKJKKJJKJJKKK")
    expect("manchester_encode", manchester_encode([1, 0, 0, 1]), [0, 1, 1, 0, 1, 0, 0, 1])
    expect("eth_frame_bytes", eth_frame_bytes(check).hex(), "55" * 7 + "d5" + check.hex() + "2639f4cb")
    expect("ps2_frame_bits of 0x1c", ps2_frame_bits(0x1C), [0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 1])
    expect("ps2_frame_bits of 0xf0", ps2_frame_bits(0xF0), [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1])
    return problems


SCENARIOS = (
    "uart_tx uart_rx uart_3m spi_ctrl_m0 spi_ctrl_m3 spi_target_m0 i2c_ctrl ws2812_tx onewire ps2_rx "
    "usb_ls_tx usb_ls_rx eth10_tx eth10_rx"
).split()


class AnyFake(Chip):
    """Becomes the correct fake for whichever scenario its image names, as a candidate's chip would."""

    def reset(self) -> None:
        self.inner: Optional[Chip] = None

    def load(self, image: bytes) -> None:
        bit = CLK_HZ / 115200
        self.inner = {
            "uart_tx": lambda: FakeUart(tx_bit=bit),
            "uart_rx": lambda: FakeUart(rx_bit=bit),
            "uart_3m": FakeUart,
            "spi_ctrl_m0": FakeSpiController,
            "spi_ctrl_m3": lambda: FakeSpiController(mode=3),
            "spi_target_m0": FakeSpiTarget,
            "i2c_ctrl": FakeI2cController,
            "ws2812_tx": FakeWs2812,
            "onewire": FakeOneWire,
            "ps2_rx": FakePs2,
            "usb_ls_tx": FakeUsbTx,
            "usb_ls_rx": FakeUsbRx,
            "eth10_tx": FakeEthTx,
            "eth10_rx": FakeEthRx,
        }[image.decode()]()

    def step(self, pins: int) -> Tuple[int, int]:
        return self.inner.step(pins)

    def host_can_push(self, ch: int) -> bool:
        return self.inner.host_can_push(ch)

    def host_push(self, ch: int, byte: int) -> None:
        self.inner.host_push(ch, byte)

    def host_pop(self, ch: int) -> Optional[int]:
        return self.inner.host_pop(ch)

    def budget(self) -> Dict[str, object]:
        return {}


def check_runner() -> List[str]:
    """`bakeoff.run` must find all 14 scenarios by name, refuse a name no bench has, and pass the correct fakes."""
    from .. import run as runner

    module = types.ModuleType("bakeoff.cand_selftest.candidate")
    module.NAME = "selftest"
    module.make_chip = AnyFake
    module.program = lambda bench_name: bench_name.encode()
    sys.modules[module.__name__] = module
    try:
        benches = runner.discover_benches()
        names = [bench.name for bench in benches]
        problems = ["no bench is named %s" % name for name in SCENARIOS if name not in names]
        problems += ["bench %s is not a scenario in the README" % name for name in names if name not in SCENARIOS]
        problems += ["two benches are named %s" % name for name in set(names) if names.count(name) > 1]
        try:
            runner.select_benches(benches, ["uart_tx", "no_such_bench"])
            problems.append("--bench accepts a name no bench has, so --strict could pass on nothing")
        except ValueError:
            pass
        if [bench.name for bench in runner.select_benches(benches, ["uart_rx", "ps2_rx"])] != ["ps2_rx", "uart_rx"]:
            problems.append("--bench does not pick exactly the benches it names")
        report = runner.run_candidate("cand_selftest", benches)
        failed = [row for row in report["rows"] if row["status"] != "pass"]
        problems += ["%s through the runner: %s" % (row["bench"], row["detail"]) for row in failed]
    finally:
        del sys.modules[module.__name__]
    return problems


def trivial_cases() -> List[Case]:
    """No bench may pass, or crash on, a chip that does nothing, holds every pin at one level, or drives noise."""
    cases: List[Case] = []
    for module in (eth10, i2c, onewire, ps2, spi, uart, usb_ls, ws2812):
        for bench in module.BENCHES:
            for label, make_chip in (
                ("does nothing", lambda: Script()),
                ("holds every pin low", lambda: Stuck(level=0)),
                ("holds every pin high", lambda: Stuck(level=1)),
                ("drives noise", lambda: Noise()),
            ):
                quick = copy.copy(bench)
                if hasattr(quick, "patience"):
                    quick.patience = min(quick.patience, 40000)  # a dead chip need not be waited for
                cases.append(("%s fails a chip that %s" % (bench.name, label), quick, make_chip, False, ""))
    return cases


def uart_cases() -> List[Case]:
    bit = CLK_HZ / 115200
    return [
        ("uart_tx passes a correct sender", UartTx(), lambda: FakeUartTx(bit), True, ""),
        ("uart_tx passes a sender 1% fast", UartTx(), lambda: FakeUartTx(bit * 0.99), True, ""),
        ("uart_tx fails a sender 5% slow", UartTx(), lambda: FakeUartTx(bit * 1.05), False, "off bit"),
        ("uart_tx fails MSB-first data", UartTx(), lambda: FakeUartTx(bit, msb_first=True), False, "decoded"),
        ("uart_tx fails a missing stop bit", UartTx(), lambda: FakeUartTx(bit, stop_level=0), False, "stop bit"),
        ("uart_rx passes a correct receiver", UartRx(), lambda: FakeUart(rx_bit=bit), True, ""),
        ("uart_rx fails a receiver 5% slow", UartRx(), lambda: FakeUart(rx_bit=bit * 1.05), False, "2% fast: "),
        ("uart_rx fails MSB-first data", UartRx(), lambda: FakeUart(rx_bit=bit, rx_msb_first=True), False, "byte 0"),
        ("uart_rx fails a receiver that waits out the stop bit", UartRx(),
         lambda: FakeUart(rx_bit=bit, full_stop=True), False, "2% fast: "),
        ("uart_rx fails a chip that drives RX", UartRx(), lambda: FakeUart(rx_bit=bit, drive_rx=True), False,
         "contention"),
        ("uart_3m passes a correct full-duplex chip", Uart3M(), lambda: FakeUart(), True, ""),
        ("uart_3m fails a sender 10% slow", Uart3M(), lambda: FakeUart(tx_bit=22), False, "off bit"),
        ("uart_3m fails a receiver 8% slow", Uart3M(), lambda: FakeUart(rx_bit=21.6), False, "RX byte"),
        ("uart_3m fails a half-duplex chip", Uart3M(), lambda: FakeUart(half_duplex=True), False, "RX "),
        ("uart_3m fails MSB-first sending", Uart3M(), lambda: FakeUart(tx_msb_first=True), False, "TX byte"),
    ]


def spi_cases() -> List[Case]:
    m0, m3, target = SpiControllerMode0(), SpiControllerMode3(), SpiTargetMode0()
    ctrl, tgt = FakeSpiController, FakeSpiTarget
    return [
        ("spi_ctrl_m0 passes a correct controller", m0, lambda: ctrl(), True, ""),
        ("spi_ctrl_m0 passes CS falling 40 cycles ahead", m0, lambda: ctrl(lead=40), True, ""),
        ("spi_ctrl_m0 passes a period of 13 and pauses between bytes", m0, lambda: ctrl(half=6, byte_gap=50), True, ""),
        ("spi_ctrl_m0 fails a period of 16", m0, lambda: ctrl(half=8), False, "SCK period is 16"),
        ("spi_ctrl_m0 fails CS only 3 cycles ahead", m0, lambda: ctrl(lead=3), False, "CS falls 3 cycles"),
        ("spi_ctrl_m0 fails LSB-first bytes", m0, lambda: ctrl(lsb_first=True), False, "MOSI byte"),
        ("spi_ctrl_m0 fails a chip that ignores the pad latency", m0, lambda: ctrl(sample_delay=9), False,
         "channel 1 byte"),
        ("spi_ctrl_m0 fails a chip that never raises CS", m0, lambda: ctrl(keep_cs=True), False, "CS is still low"),
        ("spi_ctrl_m0 fails MOSI changing on the rising edge", m0, lambda: ctrl(mosi_on_rise=True), False,
         "MOSI changes on the rising"),
        ("spi_ctrl_m0 fails a mode 3 controller", m0, lambda: ctrl(mode=3), False, "mode 0 idles low"),
        ("spi_ctrl_m0 passes a controller that starts 20,000 cycles late", m0, lambda: ctrl(start_delay=20000), True,
         ""),
        ("spi_ctrl_m3 passes a correct controller", m3, lambda: ctrl(mode=3), True, ""),
        ("spi_ctrl_m3 fails a mode 0 controller", m3, lambda: ctrl(mode=0), False, "mode 3 idles high"),
        ("spi_ctrl_m3 fails a period of 16", m3, lambda: ctrl(mode=3, half=8), False, "SCK period is 16"),
        ("spi_ctrl_m3 fails LSB-first bytes", m3, lambda: ctrl(mode=3, lsb_first=True), False, "MOSI byte"),
        ("spi_ctrl_m3 fails a chip that ignores the pad latency", m3, lambda: ctrl(mode=3, sample_delay=9), False,
         "channel 1 byte"),
        ("spi_target_m0 passes a correct target", target, lambda: tgt(), True, ""),
        ("spi_target_m0 passes a target 8 cycles slow", target, lambda: tgt(lag=8), True, ""),
        ("spi_target_m0 fails a target 11 cycles slow", target, lambda: tgt(lag=11), False, "expected"),
        ("spi_target_m0 fails MISO driven before CS falls", target, lambda: tgt(always_drive=True), False,
         "before CS falls"),
        ("spi_target_m0 fails MISO held after CS rises", target, lambda: tgt(hold=True), False, "still drives MISO"),
        ("spi_target_m0 fails LSB-first bytes", target, lambda: tgt(lsb_first=True), False, "MISO byte"),
        ("spi_target_m0 fails a chip that pushes nothing", target, lambda: tgt(no_push=True), False, "channel 1 got 0"),
    ]


def i2c_cases() -> List[Case]:
    bench, fake = I2cController(), FakeI2cController
    return [
        ("i2c_ctrl passes a correct controller", bench, lambda: fake(), True, ""),
        ("i2c_ctrl passes the slowest legal clock", bench, lambda: fake(low=120, high=77), True, ""),
        ("i2c_ctrl passes the fastest legal clock", bench, lambda: fake(low=111, high=36), True, ""),
        ("i2c_ctrl fails a chip that does not wait for a stretch", bench, lambda: fake(no_wait=True), False,
         "it must wait"),
        ("i2c_ctrl fails a chip that drives SCL high", bench, lambda: fake(push_pull=True), False, "open drain"),
        ("i2c_ctrl fails SCL low for 60 cycles", bench, lambda: fake(low=60, high=100), False, "SCL is low for 60"),
        ("i2c_ctrl fails SCL high for 30 cycles", bench, lambda: fake(low=140, high=27), False, "SCL is high for 30"),
        ("i2c_ctrl fails a period of 120 cycles", bench, lambda: fake(low=80, high=37), False, "SCL period is 120"),
        ("i2c_ctrl fails a period of 250 cycles", bench, lambda: fake(low=150, high=97), False, "SCL period is 250"),
        ("i2c_ctrl fails 3 cycles of SDA setup", bench, lambda: fake(setup=3), False, "SDA changes 3 cycles before"),
        ("i2c_ctrl fails SDA changing while SCL is high", bench, lambda: fake(sda_early=True), False, "bus event"),
        ("i2c_ctrl fails a STOP before the read", bench, lambda: fake(stop_start=True), False,
         "run A: bus event 5: expected START, saw STOP"),
        ("i2c_ctrl fails an ACK on the last byte read", bench, lambda: fake(ack_last=True), False,
         "run A: bus event 10"),
        ("i2c_ctrl fails a chip that carries on after a NACK", bench, lambda: fake(ignore_nack=True), False,
         "run B: bus event 2"),
        ("i2c_ctrl fails status 0x00 after a NACK", bench, lambda: fake(always_ok=True), False,
         "run B: channel 2 (status) byte 0: expected 01, got 00"),
        ("i2c_ctrl fails a missing status byte", bench, lambda: fake(no_status=True), False,
         "channel 2 (status) got 0"),
    ]


def ws2812_cases() -> List[Case]:
    bench, fake = Ws2812Tx(), FakeWs2812
    return [
        ("ws2812_tx passes a correct sender", bench, lambda: fake(), True, ""),
        ("ws2812_tx passes times at the edge of the tolerance", bench,
         lambda: fake(t0h=33, t0l=42, t1h=39, t1l=36), True, ""),
        ("ws2812_tx fails a 0 that is high for 36 cycles", bench, lambda: fake(t0h=36), False, "high for 36"),
        ("ws2812_tx fails a 1 that is low for 40 cycles", bench, lambda: fake(t1l=40), False, "low for 40"),
        ("ws2812_tx fails LSB-first bytes", bench, lambda: fake(lsb_first=True), False, "byte 0: host gave"),
        ("ws2812_tx fails a pause between bytes", bench, lambda: fake(byte_gap=100), False, "is low for"),
        ("ws2812_tx fails a line that rests high", bench, lambda: fake(idle_high=True), False, "still high at the end"),
        ("ws2812_tx fails a pulse 2,000 cycles after the last bit", bench, lambda: fake(encore=2000), False,
         "low for 20"),
    ]


def ps2_cases() -> List[Case]:
    bench, fake = Ps2Rx(), FakePs2
    return [
        ("ps2_rx passes a correct listener", bench, lambda: fake(), True, ""),
        ("ps2_rx fails a chip that skips the parity check", bench, lambda: fake(no_parity_check=True), False,
         "channel 2 got 0"),
        ("ps2_rx fails a chip that keeps the bad byte", bench, lambda: fake(keep_bad=True), False, "channel 1 byte 5"),
        ("ps2_rx fails MSB-first data", bench, lambda: fake(msb_first=True), False, "channel 1 byte 0"),
        ("ps2_rx fails sampling on the rising edge", bench, lambda: fake(rising=True), False, "channel"),
        ("ps2_rx fails a chip that pulls DATA low", bench, lambda: fake(pull_data=True), False, "must only listen"),
    ]


def onewire_cases() -> List[Case]:
    bench, fake = OneWire(), FakeOneWire
    return [
        ("onewire passes a correct controller", bench, lambda: fake(), True, ""),
        ("onewire passes the longest reset and the shortest slots", bench,
         lambda: fake(reset=57600, one=60, zero=3600, slot=3660), True, ""),
        ("onewire fails a 400 us reset", bench, lambda: fake(reset=24000), False, "reset is low for 400.0"),
        ("onewire fails a written 1 that is low for 20 us", bench, lambda: fake(one=1200), False,
         "write slot 0 is low for 20.0"),
        ("onewire fails a written 0 that is low for 50 us", bench, lambda: fake(zero=3000), False,
         "write slot 2 is low for 50.0"),
        ("onewire fails slots 60 us apart", bench, lambda: fake(slot=3630, zero=3600), False, "needs 61"),
        ("onewire fails no high time between slots", bench, lambda: fake(slot=3930), False, "less than 1 us"),
        ("onewire fails the command MSB first", bench, lambda: fake(msb_first=True), False, "chip wrote cc"),
        ("onewire fails sampling 35 us into the slot", bench, lambda: fake(sample=2100), False, "run A: channel 1"),
        ("onewire fails a chip that drives DQ high", bench, lambda: fake(push_pull=True), False, "open drain"),
        ("onewire fails a chip that skips the CRC", bench, lambda: fake(no_crc=True), False,
         "run B: channel 2 (status) byte 1: expected 01, got 00"),
        ("onewire fails a chip that carries on with nobody there", bench, lambda: fake(ignore_absent=True), False,
         "run C: chip sent"),
        ("onewire fails a missing presence byte", bench, lambda: fake(no_presence_byte=True), False,
         "run A: channel 2 (status) got 1"),
        ("onewire fails a chip that does nothing", bench, lambda: Script(), False, "never pulled DQ low"),
    ]


def usb_cases() -> List[Case]:
    tx, rx = UsbLsTx(), UsbLsRx()
    send, listen = FakeUsbTx, FakeUsbRx
    return [
        ("usb_ls_tx passes a correct sender", tx, lambda: send(), True, ""),
        ("usb_ls_tx passes a bare PID for the packet with no data", tx, lambda: send(bare_handshake=True), True, ""),
        ("usb_ls_tx passes D- 2 cycles behind D+", tx, lambda: send(skew=2), True, ""),
        ("usb_ls_tx fails D- 5 cycles behind D+", tx, lambda: send(skew=5), False, "D- edge at cycle"),
        ("usb_ls_tx fails a wrong CRC", tx, lambda: send(bad_crc=True), False, "CRC bytes are"),
        ("usb_ls_tx fails the CRC high byte first", tx, lambda: send(crc_high_first=True), False, "CRC bytes are"),
        ("usb_ls_tx fails a sender that does not stuff", tx, lambda: send(no_stuff=True), False, "seven 1s in a row"),
        ("usb_ls_tx fails MSB-first bytes", tx, lambda: send(msb_first=True), False, "byte 0: expected 4b"),
        ("usb_ls_tx fails a bit time of 40.5 cycles", tx, lambda: send(bit=40.5), False, "off the grid"),
        ("usb_ls_tx fails one edge 4 cycles late", tx, lambda: send(late_edge=True), False, "off the grid"),
        ("usb_ls_tx fails SE0 for one bit time", tx, lambda: send(se0_bits=1), False, "SE0 lasts 1 bit"),
        ("usb_ls_tx fails no driven J after SE0", tx, lambda: send(no_j=True), False, "does not drive J"),
        ("usb_ls_tx fails a chip that keeps driving", tx, lambda: send(keep_driving=True), False, "still drives"),
        ("usb_ls_rx passes a correct listener", rx, lambda: listen(), True, ""),
        ("usb_ls_rx fails a listener that never resynchronises", rx, lambda: listen(free_run=True), False,
         "channel 1"),
        ("usb_ls_rx fails a listener that does not unstuff", rx, lambda: listen(no_unstuff=True), False,
         "(packet 1,"),
        ("usb_ls_rx fails a listener that skips the CRC", rx, lambda: listen(always_good=True), False,
         "channel 2 byte 6: expected 01, got 00"),
        ("usb_ls_rx fails a byte count without PID and CRC", rx, lambda: listen(count_data_only=True), False,
         "channel 2 byte 1: expected 0b, got 08"),
        ("usb_ls_rx fails a chip that drives the bus", rx, lambda: listen(drive=True), False, "must never drive"),
    ]


def eth_cases() -> List[Case]:
    tx, rx = Eth10Tx(), Eth10Rx()
    send, listen = FakeEthTx, FakeEthRx
    return [
        ("eth10_tx passes a correct sender", tx, lambda: send(), True, ""),
        ("eth10_tx passes TX+ high for 12 cycles and a gap of exactly 576", tx, lambda: send(tail=12, gap=564), True,
         ""),
        ("eth10_tx fails a wrong FCS", tx, lambda: send(bad_fcs=True), False, "FCS is"),
        ("eth10_tx fails the FCS high byte first", tx, lambda: send(fcs_high_first=True), False, "FCS is"),
        ("eth10_tx fails six bytes of preamble", tx, lambda: send(preamble=6), False, "preamble byte 6"),
        ("eth10_tx fails MSB-first bytes", tx, lambda: send(msb_first=True), False, "preamble byte 0"),
        ("eth10_tx fails one half bit of 4 cycles", tx, lambda: send(stretch=True), False,
         "bit 200 at cycle"),
        ("eth10_tx fails TX+ high for 30 cycles at the end", tx, lambda: send(tail=30), False, "high for 30 cycles"),
        ("eth10_tx fails a frame with no end marker", tx, lambda: send(tail=0), False, "must first stay high"),
        ("eth10_tx fails frames 400 cycles apart", tx, lambda: send(gap=382), False, "starts 400 cycles after"),
        ("eth10_tx fails TX- held low", tx, lambda: send(single_ended=True), False, "TX- is the complement"),
        ("eth10_tx fails a chip that sends one frame", tx, lambda: send(one_frame=True), False, "1 frames on the wire"),
        ("eth10_rx passes a correct listener", rx, lambda: listen(), True, ""),
        ("eth10_rx fails a listener that never resynchronises", rx, lambda: listen(free_run=True), False,
         "channel 1"),
        ("eth10_rx fails a listener that skips the FCS", rx, lambda: listen(always_good=True), False,
         "channel 2 byte 6: expected 01, got 00"),
        ("eth10_rx fails a length without the FCS", rx, lambda: listen(length_without_fcs=True), False,
         "channel 2 byte 1: expected 40, got 3c"),
        ("eth10_rx fails a listener that pushes the 0xD5", rx, lambda: listen(keep_sfd=True), False,
         "channel 1 byte 0: sent"),
        ("eth10_rx fails MSB-first bytes", rx, lambda: listen(msb_first=True), False, "channel 1 byte 0"),
    ]


def main() -> int:
    cases: List[Case] = []
    cases += uart_cases()
    cases += spi_cases()
    cases += i2c_cases()
    cases += ws2812_cases()
    cases += ps2_cases()
    cases += onewire_cases()
    cases += usb_cases()
    cases += eth_cases()
    cases += trivial_cases()
    failures: List[str] = []
    checks = 0
    for name, check in (
        ("the pads cost three cycles", check_sim_latency),
        ("peek agrees with tick", check_peek),
        ("the stimulus lands on the clock grid", check_stimulus),
        ("the reference CRCs and line codes match known vectors", check_codecs),
        ("the runner finds all 14 scenarios, refuses an unknown name and passes the correct fakes", check_runner),
    ):
        found = check()
        shown = "  (%s)" % "; ".join(found[:3]) if found else ""
        print("%s  %s%s" % ("FAIL" if found else "ok  ", name, shown))
        checks += 1
        if found:
            failures.append(name)  # one failed check, however many problems it names
    for label, bench, make_chip, should_pass, expect in cases:
        result = bench.run(make_chip, b"")
        ok = result.passed == should_pass and expect in result.detail
        if should_pass and result.detail:
            ok = False  # a passing bench has nothing to complain about
        print("%s  %s%s" % ("ok  " if ok else "FAIL", label, "" if ok else "  (%s)" % (result.detail or "passed")))
        checks += 1
        if not ok:
            failures.append(label)
    print("\n%d checks, %d failed" % (checks, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
