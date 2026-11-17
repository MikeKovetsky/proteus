"""I2C scenarios. See bakeoff/README.md for what each one asks of the chip."""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from ..chip import CH_RX, CH_STATUS, CH_TX, Chip
from ..sim import Bench, Result, Sim, payload, us
from ..stim import first_difference, first_of, run, run_until

Event = Tuple  # ("start",), ("stop",) or ("byte", value, acked)


def describe(event: Optional[Event]) -> str:
    if event is None:
        return "nothing"
    if event[0] == "byte":
        return "byte %02x %s" % (event[1], "ACK" if event[2] else "NACK")
    return event[0].upper()


class I2cTarget:
    """The bench's I2C target, and its bus monitor. It answers in the same cycle (no delay).

    It watches the bus as wired (its own pulls included), logs every START, STOP and nine-bit byte, and
    when `present` it ACKs its address, ACKs written bytes and sends `read_data` when read. After the ACK
    bit of its address-with-read byte it holds SCL low for `stretch` cycles, counted from that falling edge.
    """

    def __init__(self, pins: Dict[str, int], address: int, read_data: bytes, present: bool, stretch: int) -> None:
        self.scl, self.sda = pins["SCL"], pins["SDA"]
        self.address = address
        self.read_data = read_data
        self.present = present
        self.stretch = stretch
        self.log: List[Event] = []
        self.stretched: Optional[Tuple[int, int]] = None  # the cycles SCL was held, first and one past last
        self._release_scl_at: Optional[int] = None
        self._last = (1, 1)
        self._active = False
        self._mode = "idle"  # address, write (the target receives), read (the target sends) or ignore
        self._next_mode = "ignore"
        self._count = 0  # rising SCL edges seen in this byte
        self._shift = 0
        self._sending = 0xFF
        self._read_index = 0

    def _sda(self, sim: Sim, level: int) -> None:
        if level:
            sim.release(self.sda)
        else:
            sim.set(self.sda, 0)

    def __call__(self, sim: Sim) -> None:
        if self._release_scl_at is not None and sim.cycle >= self._release_scl_at:
            sim.release(self.scl)
            self._release_scl_at = None
        line = sim.peek()
        scl, sda = (line >> self.scl) & 1, (line >> self.sda) & 1
        last_scl, last_sda = self._last
        self._last = (scl, sda)

        if last_scl and scl and last_sda != sda:
            # SDA moved while SCL stayed high: START on a fall, STOP on a rise.
            self.log.append(("stop",) if sda else ("start",))
            self._active = not sda
            self._mode = "address"
            self._count = 0
            self._shift = 0
            sim.release(self.sda)
            return
        if not self._active:
            return

        if scl and not last_scl:
            if self._count < 8:
                self._shift = (self._shift << 1) | sda
            else:
                self.log.append(("byte", self._shift, not sda))
                if self._mode == "read" and sda:
                    self._next_mode = "ignore"  # the controller's NACK ends the read
            self._count += 1
        elif last_scl and not scl:
            if self._count == 8:
                self._ack_slot(sim)
            elif self._count == 9:
                self._byte_done(sim)
            elif self._mode == "read" and self._count:
                self._sda(sim, (self._sending >> (7 - self._count)) & 1)

    def _ack_slot(self, sim: Sim) -> None:
        """The eighth clock has fallen: drive or release SDA for the ACK bit."""
        if self._mode == "address":
            mine = self.present and (self._shift >> 1) == self.address
            self._next_mode = ("read" if self._shift & 1 else "write") if mine else "ignore"
            self._sda(sim, 0 if mine else 1)
        elif self._mode == "write":
            self._next_mode = "write"
            self._sda(sim, 0)
        else:
            if self._mode == "read":
                self._next_mode = "read"
            self._sda(sim, 1)

    def _byte_done(self, sim: Sim) -> None:
        """The ninth clock has fallen: start the next byte."""
        stretch_now = self._mode == "address" and self._next_mode == "read" and self.stretch > 0
        self._mode = self._next_mode
        self._count = 0
        self._shift = 0
        if self._mode == "read":
            index = self._read_index
            self._sending = self.read_data[index] if index < len(self.read_data) else 0xFF
            self._read_index += 1
            self._sda(sim, self._sending >> 7)
        else:
            self._sda(sim, 1)
        if stretch_now and self.stretched is None:
            sim.set(self.scl, 0)
            self._release_scl_at = sim.cycle + self.stretch
            self.stretched = (sim.cycle, sim.cycle + self.stretch)


def check_i2c_timing(wave: List[int], pins: Dict[str, int]) -> List[str]:
    """Judge the bus timing the README gives, on the wired levels of SCL and SDA."""
    scl_mask, sda_mask = 1 << pins["SCL"], 1 << pins["SDA"]
    low_times: List[str] = []
    high_times: List[str] = []
    periods: List[str] = []
    setups: List[str] = []
    rose: Optional[int] = None  # when SCL last rose
    fell: Optional[int] = None  # when SCL last fell
    sda_moved = -1000
    clock = 0  # which clock of the nine-bit byte comes next
    for c in range(1, len(wave)):
        scl0, scl1 = wave[c - 1] & scl_mask, wave[c] & scl_mask
        if (wave[c - 1] ^ wave[c]) & sda_mask:
            sda_moved = c
            if scl0 and scl1:
                clock = 0  # START or STOP: the next rising edge opens a new byte
                if rose is not None and c - rose < 6:
                    setups.append(
                        "%s at cycle %d comes %d cycles after SCL rises, needs 6 cycles of setup"
                        % ("STOP" if wave[c] & sda_mask else "START", c, c - rose)
                    )
            elif scl1:
                setups.append("SDA changes in the cycle SCL rises, at %d (needs 6 cycles of setup)" % c)
        if scl1 and not scl0:
            if 0 < c - sda_moved < 6:
                setups.append("SDA changes %d cycles before SCL rises at cycle %d, needs 6" % (c - sda_moved, c))
            if fell is not None and c - fell < 78:
                low_times.append("SCL is low for %d cycles before cycle %d, needs 78" % (c - fell, c))
            # Inside a byte nobody stretches, so the period must hold. Between bytes the chip may pause.
            if clock % 9 and rose is not None and not 150 <= c - rose <= 200:
                periods.append("SCL period is %d cycles at cycle %d (150 to 200 allowed)" % (c - rose, c))
            clock += 1
            rose = c
        elif scl0 and not scl1:
            if rose is not None and c - rose < 36:
                high_times.append("SCL is high for %d cycles before cycle %d, needs 36" % (c - rose, c))
            fell = c
    return first_of(setups) + first_of(low_times) + first_of(high_times) + first_of(periods)


class I2cController(Bench):
    """The chip is an I2C controller at 400 kHz: write 3 bytes, repeated START, read 4 bytes, STOP.

    Run A has a target that ACKs and stretches once. Run B has no target. Both use the same image and
    the same host message, `[addr7][3][3 bytes][4]`.

    Lenient readings, where the README is silent:
    1. The period of 150 to 200 cycles is measured between rising SCL edges inside a nine-bit byte. Between
       bytes, and around START and STOP, the chip may pause for as long as it likes. Every low time must
       still be at least 78 cycles and every high time at least 36.
    2. SDA may change in the same cycle SCL falls (zero hold). It must then stay put for 6 cycles before
       SCL rises. The README's 6 cycles of setup hold for START and STOP too: SCL must have been high for 6
       cycles when SDA moves. No hold time after a START is asked for.
    3. The stretch comes after the ACK bit of the address-with-read byte: the target holds SCL low for
       1,200 cycles from that falling edge. A chip that pulls SCL low again inside that time did not wait.
    4. The order of the pushes to channels 1 and 2 is not judged, only their contents.
    5. The chip may start whenever it likes within `patience` cycles of reset.
    """

    name = "i2c_ctrl"
    pins = {"SCL": 0, "SDA": 1}
    address = 0x53
    patience = 200000

    def run(self, make_chip: Callable[[], Chip], image: bytes) -> Result:
        scl, sda = self.pins["SCL"], self.pins["SDA"]
        bus = (1 << scl) | (1 << sda)
        to_write = payload(20, 3)
        to_read = payload(21, 4)
        message = bytes([self.address, len(to_write)]) + to_write + bytes([len(to_read)])
        write_address, read_address = self.address << 1, (self.address << 1) | 1
        problems: List[str] = []
        cycles = 0
        for label, present in (("run A", True), ("run B", False)):
            if present:
                expected: List[Event] = [("start",), ("byte", write_address, True)]
                expected += [("byte", byte, True) for byte in to_write]
                expected += [("start",), ("byte", read_address, True)]
                expected += [("byte", byte, i + 1 < len(to_read)) for i, byte in enumerate(to_read)]
                expected += [("stop",)]
                want_data, want_status = to_read, b"\x00"
            else:
                expected = [("start",), ("byte", write_address, False), ("stop",)]
                want_data, want_status = b"", b"\x01"
            sim = Sim(make_chip(), image)
            sim.send(CH_TX, message)
            target = I2cTarget(self.pins, self.address, to_read, present, int(us(20)))
            run_until(sim, lambda s: len(s.from_chip[CH_STATUS]) >= 1, self.patience, target)
            run(sim, 1000, target)  # time to finish a STOP that comes after the status byte
            cycles += sim.cycle

            found: List[str] = []
            for pin, pin_name in ((scl, "SCL"), (sda, "SDA")):
                if sim.chip_drove_high & (1 << pin):
                    found.append("chip drove %s high, and it is open drain" % pin_name)
            for index in range(max(len(expected), len(target.log))):
                want = expected[index] if index < len(expected) else None
                saw = target.log[index] if index < len(target.log) else None
                if want != saw:
                    found.append("bus event %d: expected %s, saw %s" % (index, describe(want), describe(saw)))
                    break
            if target.stretched:
                first, end = target.stretched
                again = [
                    c for c in range(first + 1, min(end, len(sim.wave)))
                    if sim.oe_wave[c] & ~sim.oe_wave[c - 1] & (1 << scl)
                ]
                if again:
                    found.append(
                        "chip pulls SCL low again at cycle %d while the target holds it until %d: it must wait"
                        % (again[0], end)
                    )
            difference = first_difference(want_data, sim.received(CH_RX), "target sent")
            if difference:
                found.append("channel 1 %s" % difference)
            difference = first_difference(want_status, sim.received(CH_STATUS), "expected")
            if difference:
                found.append("channel 2 (status) %s" % difference)
            found += check_i2c_timing(sim.wave, self.pins)
            if sim.wave[-1] & bus != bus:
                found.append("the bus is not idle (both lines high) at the end")
            if sim.contention:
                found.append("%d pin-cycles of contention" % sim.contention)
            problems += ["%s: %s" % (label, problem) for problem in found]
        return Result(self.name, not problems, "; ".join(problems[:3]), cycles)


BENCHES = [I2cController()]
