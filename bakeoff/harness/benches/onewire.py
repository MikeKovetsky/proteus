"""1-Wire scenario. See bakeoff/README.md for what it asks of the chip."""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from ..chip import CH_RX, CH_STATUS, Chip
from ..codec import crc8_onewire
from ..sim import Bench, Result, Sim, payload, us
from ..stim import bits_lsb_first, bytes_lsb_first, first_difference, first_of, low_runs, run, run_until

RESET_MIN, RESET_MAX = int(us(480)), int(us(960))
ONE_MIN, ONE_MAX = int(us(1)), int(us(15))        # the low time of a written 1, and of every read slot
ZERO_MIN, ZERO_MAX = int(us(60)), int(us(120))    # the low time of a written 0
SLOT_SPACING = int(us(61))
RECOVERY = int(us(1))
PRESENCE_DELAY, PRESENCE_LENGTH = int(us(30)), int(us(120))
READ_HOLD = int(us(30))
READ_ROM = 0x33


class OneWireDevice:
    """The bench's 1-Wire device. It answers in the same cycle (no delay). With `rom` None, nobody is there.

    Any low time of 240 µs or more counts as a reset for the device, so a short reset still gets an
    answer and the bench can name the real fault. The presence pulse runs from 30 µs to 150 µs after the
    line rises. After it, the device counts falling edges as slots: 8 written bits, then 64 read slots.
    In a read slot whose bit is 0 it holds the line low until 30 µs after the slot began.
    """

    def __init__(self, pin: int, rom: Optional[bytes]) -> None:
        self.pin = pin
        self.bits = bits_lsb_first(rom) if rom is not None else None
        self.presence: Optional[Tuple[int, int]] = None  # first cycle and one past the last
        self._presence_at: Optional[int] = None
        self._release_at: Optional[int] = None
        self._slots_from: Optional[int] = None  # falling edges count as slots from this cycle on
        self._slot = 0
        self._last = 1
        self._low_since = 0

    def __call__(self, sim: Sim) -> None:
        cycle = sim.cycle
        if self._release_at is not None and cycle >= self._release_at:
            sim.release(self.pin)
            self._release_at = None
        if self._presence_at is not None and cycle >= self._presence_at:
            sim.set(self.pin, 0)
            self._presence_at = None
            self._release_at = cycle + PRESENCE_LENGTH
            self.presence = (cycle, cycle + PRESENCE_LENGTH)
            self._slots_from = cycle + PRESENCE_LENGTH
            self._slot = 0
        line = (sim.peek() >> self.pin) & 1
        last, self._last = self._last, line
        if last and not line:
            self._low_since = cycle
            if self._slots_from is not None and cycle >= self._slots_from and self.bits is not None:
                read = self._slot - 8
                if 0 <= read < len(self.bits) and not self.bits[read]:
                    sim.set(self.pin, 0)
                    self._release_at = cycle + READ_HOLD
                self._slot += 1
        elif line and not last and cycle - self._low_since >= RESET_MIN // 2:
            self._slots_from = None
            if self.bits is not None:
                self._presence_at = cycle + PRESENCE_DELAY


def microseconds(cycles: int) -> str:
    """A time in cycles as text. Messages stay ASCII, so the unit is written "us"."""
    return "%.1f us" % (cycles / us(1))


class OneWire(Bench):
    """The chip is a 1-Wire controller: reset, write 0x33, read 64 bits, check the CRC-8.

    Three runs with the same image: a device with a good CRC, one with a bad CRC, and no device.

    Lenient readings, where the README is silent:
    1. What the chip does is read from when it pulls DQ low, so every time in the README is the chip's own
       low time or the spacing of its falling edges. The 1 µs of high time between slots is measured on
       the wire, where a device may still be holding a 0.
    2. The first slot may start any time after the presence pulse ends (150 µs after the reset is
       released), as long as the wire has been high for 1 µs.
    3. The chip may sample a read slot whenever it likes. The device's 0 lasts until 30 µs after the slot
       began, and a 1 is the pull-up.
    4. The order of the pushes to channels 1 and 2 is not judged, only their contents. In run B the chip
       still pushes the 8 bytes it read.
    5. The chip may start whenever it likes, and may be slow between slots: runs A and B end `patience`
       cycles after reset at the latest. In run C the bench watches for 1 ms after the status byte to see
       that the chip has stopped.
    """

    name = "onewire"
    pins = {"DQ": 0}
    patience = 1000000

    def run(self, make_chip: Callable[[], Chip], image: bytes) -> Result:
        pin = self.pins["DQ"]
        mask = 1 << pin
        serial = bytes([0x28]) + payload(50, 6)
        good_rom = serial + bytes([crc8_onewire(serial)])
        bad_rom = good_rom[:3] + bytes([good_rom[3] ^ 0x10]) + good_rom[4:]
        problems: List[str] = []
        cycles = 0
        for label, rom, want_status in (
            ("run A", good_rom, b"\x00\x00"),
            ("run B", bad_rom, b"\x00\x01"),
            ("run C", None, b"\x01"),
        ):
            want_data = rom if rom is not None else b""
            sim = Sim(make_chip(), image)
            device = OneWireDevice(pin, rom)
            run_until(
                sim,
                lambda s: len(s.from_chip[CH_STATUS]) >= len(want_status) and len(s.from_chip[CH_RX]) >= len(want_data),
                self.patience,
                device,
            )
            run(sim, us(1000) if rom is None else 5000, device)
            cycles += sim.cycle

            found: List[str] = []
            if sim.chip_drove_high & mask:
                found.append("chip drove DQ high, and it is open drain")
            line = [1 if w & mask else 0 for w in sim.wave]
            # The chip's own low times: where it drives the pin and the wire is low.
            pulses = low_runs([0 if (oe & mask and not level) else 1 for oe, level in zip(sim.oe_wave, line)])
            if not pulses:
                problems.append("%s: chip never pulled DQ low in %d cycles" % (label, sim.cycle))
                break  # the same image will do the same in the other runs
            reset_length = pulses[0][1]
            if not RESET_MIN <= reset_length <= RESET_MAX:
                found.append("reset is low for %s (480 to 960 allowed)" % microseconds(reset_length))
            slots = pulses[1:]
            if rom is None:
                if slots:
                    found.append("chip sent %d slots after a reset that nobody answered" % len(slots))
            else:
                found += self.check_slots(line, slots, device)
            difference = first_difference(want_data, sim.received(CH_RX), "device sent")
            if difference:
                found.append("channel 1 %s" % difference)
            difference = first_difference(want_status, sim.received(CH_STATUS), "expected")
            if difference:
                found.append("channel 2 (status) %s" % difference)
            if not line[-1]:
                found.append("DQ is still low at the end")
            if sim.contention:
                found.append("%d pin-cycles of contention" % sim.contention)
            problems += ["%s: %s" % (label, problem) for problem in found]
        return Result(self.name, not problems, "; ".join(problems[:3]), cycles)

    @staticmethod
    def check_slots(line: List[int], slots: List[Tuple[int, int]], device: OneWireDevice) -> List[str]:
        """Judge the 8 write slots and 64 read slots that follow the reset."""
        found: List[str] = []
        if len(slots) != 72:
            found.append("%d slots after the reset, expected 72 (8 written, 64 read)" % len(slots))
        if slots and device.presence and slots[0][0] < device.presence[1]:
            found.append(
                "the first slot starts at cycle %d, before the presence pulse ends at %d"
                % (slots[0][0], device.presence[1])
            )
        written: List[int] = []
        shapes: List[str] = []
        spacing: List[str] = []
        for index, (start, length) in enumerate(slots[:72]):
            if index and start - slots[index - 1][0] < SLOT_SPACING:
                spacing.append(
                    "slot %d starts %s after slot %d, needs 61"
                    % (index, microseconds(start - slots[index - 1][0]), index - 1)
                )
            elif not all(line[max(0, start - RECOVERY) : start]):
                spacing.append(
                    "slot %d starts at cycle %d with less than 1 us of high time before it" % (index, start)
                )
            if index < 8:
                if ONE_MIN <= length <= ONE_MAX:
                    written.append(1)
                elif ZERO_MIN <= length <= ZERO_MAX:
                    written.append(0)
                else:
                    written.append(1 if length < (ONE_MAX + ZERO_MIN) // 2 else 0)
                    shapes.append(
                        "write slot %d is low for %s (a 1 is 1 to 15, a 0 is 60 to 120)" % (index, microseconds(length))
                    )
            elif not ONE_MIN <= length <= ONE_MAX:
                shapes.append(
                    "read slot %d: chip holds DQ low for %s (1 to 15 allowed)" % (index - 8, microseconds(length))
                )
        if len(written) == 8 and bytes_lsb_first(written)[0] != READ_ROM:
            found.append("chip wrote %02x, expected %02x" % (bytes_lsb_first(written)[0], READ_ROM))
        return found + first_of(shapes) + first_of(spacing)


BENCHES = [OneWire()]
