# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The devices to be learned. This is the truth, and only the board and the judge may read it.

Each is written as behaviour over a finite state and enumerated into the table that board.cpp plays at bit
level. The parts are invented. They are modelled on the habits of real ones (latches, modes, pointer
registers, unlock commands), and none copies a real part's command set or identity."""

import random
from array import array

SELECT, DESELECT = 256, 257  # only for `inputs`
TOO_MANY = 20000


class Table:
    """A device as board.cpp takes it."""

    def __init__(self, name, bus, states, out, next_, a, b, c=None, ack=None, idle_next=None):
        self.name, self.bus, self.states, self.n = name, bus, states, len(states)
        self.out, self.next, self.a, self.b, self.c, self.ack = out, next_, a, b, c, ack
        self.idle_next = idle_next
        self.mask = None
        self.quirks = {}

    def but(self, name, mask=None, **quirks):
        """The same table with something wrong with it: a negative control."""
        other = Table(name, self.bus, self.states, self.out, self.next, self.a, self.b, self.c, self.ack,
                      self.idle_next)
        other.mask, other.quirks = mask, quirks
        return other


def enumerate_states(part, steps):
    states, index = [part.reset()], {part.reset(): 0}
    rows = []
    while len(rows) < len(states):
        row = []
        for state in steps(states[len(rows)]):
            if state not in index:
                index[state] = len(states)
                states.append(state)
                if len(states) > TOO_MANY:
                    raise ValueError("%s has too many states" % part.name)
            row.append(index[state])
        rows.append(row)
    idled = getattr(part, "idled", None)
    idle_next = None
    if idled:
        idle_next = array("i", [index.get(idled(state), i) for i, state in enumerate(states)])
    return states, rows, idle_next


def spi_table(part):
    def steps(state):
        return [part.byte(state, x) for x in range(256)] + [part.select(state), part.deselect(state)]

    states, rows, idle_next = enumerate_states(part, steps)
    return Table(part.name, "spi", states, bytes(part.sends(s) for s in states),
                 array("i", [t for row in rows for t in row[:256]]),
                 array("i", [row[256] for row in rows]), array("i", [row[257] for row in rows]),
                 idle_next=idle_next)


def i2c_table(part):
    acks = []

    def steps(state):
        results = [part.write(state, x) for x in range(256)] + [part.start(state, x) for x in range(256)]
        acks.extend(1 if ack else 0 for _, ack in results)
        return [t for t, _ in results] + [part.read(state, False), part.read(state, True), part.stop(state)]

    states, rows, idle_next = enumerate_states(part, steps)
    return Table(part.name, "i2c", states, bytes(part.sends(s) for s in states),
                 array("i", [t for row in rows for t in row[:512]]),
                 array("i", [row[512] for row in rows]), array("i", [row[513] for row in rows]),
                 array("i", [row[514] for row in rows]), ack=bytes(acks), idle_next=idle_next)


# ---- the flash

READY, WRITE_ENABLED, TEST, ASLEEP = "ready", "write enabled", "test", "asleep"
FLASH_STATUS = {READY: 0x00, WRITE_ENABLED: 0x02, TEST: 0x80}
FLASH_ID = (0xC7, 0x3A, 0x14)
FLASH_KEY = (0x4D, 0x4B, 0x56, 0x31)
FLASH_ONE_BYTE = (0x04, 0x06, 0x5E, 0x66, 0x99, 0xAB, 0xB9)


class Flash:
    """An SPI NOR flash's control half. Its data sheet would give
        05 status (00 ready, 02 write enabled, 40 reset armed), repeated
        9F identity C7 3A 14
        06 04 write enable and disable, which take effect at deselect and only as one-byte transactions
        B9 AB sleep and wake: asleep it ignores all but AB
        66 99 software reset: 99 counts only as the transaction right after 66
    and leave out
        5E    from write enabled only, the factory test mode (status 80, and a fourth identity byte 2C)
        E7    in test mode only, reads the key 4D 4B 56 31"""

    name = "flash"
    documented = (0x04, 0x05, 0x06, 0x66, 0x99, 0x9F, 0xAB, 0xB9)
    hidden = (0x5E, 0xE7)

    def reset(self):
        return (READY, False, None)

    def select(self, state):
        return state[:2] + (("command", 0),)

    def byte(self, state, x):
        mode, armed, doing = state
        if doing is None:
            return state
        what, at = doing
        if what == "command":
            if mode == ASLEEP and x != 0xAB:
                return (mode, armed, ("ignore", 0))
            if x == 0x05:
                return (mode, armed, ("status", 0))
            if x == 0x9F:
                return (mode, armed, ("id", 0))
            if x == 0xE7 and mode == TEST:
                return (mode, armed, ("key", 0))
            if x in FLASH_ONE_BYTE:
                return (mode, armed, ("do", x))
            return (mode, armed, ("ignore", 0))
        if what == "do":
            return (mode, armed, ("ignore", 0))
        if what in ("id", "key"):
            return (mode, armed, (what, min(at + 1, 4)))
        return state

    def sends(self, state):
        mode, armed, doing = state
        if doing is None:
            return 0xFF
        what, at = doing
        if what == "status":
            return FLASH_STATUS[mode] | (0x40 if armed else 0)
        if what == "id":
            return (FLASH_ID + (0x2C if mode == TEST else 0, 0))[at]
        if what == "key":
            return (FLASH_KEY + (0,))[at]
        return 0xFF

    def deselect(self, state):
        mode, armed, doing = state
        if doing is None:
            return state
        command = doing[1] if doing[0] == "do" else None
        if command == 0x06 and mode == READY:
            mode = WRITE_ENABLED
        elif command == 0x04 and mode in (WRITE_ENABLED, TEST):
            mode = READY
        elif command == 0x5E and mode == WRITE_ENABLED:
            mode = TEST
        elif command == 0xB9:
            mode = ASLEEP
        elif command == 0xAB and mode == ASLEEP:
            mode = READY
        elif command == 0x99 and armed:
            mode = READY
        return (mode, command == 0x66, None)

    def idled(self, state):
        """For the negative control whose write enable runs out."""
        return (READY,) + state[1:] if state[0] == WRITE_ENABLED else state


# ---- the sensor

STANDBY, RUN, SERVICE = "standby", "run", "service"
SENSOR_VALUES = {
    (0x00, STANDBY): (0x80, 0x00), (0x00, RUN): (0x19, 0x60), (0x00, SERVICE): (0x19, 0x60),
    (0x01, STANDBY): (0x00,), (0x01, RUN): (0x01,), (0x01, SERVICE): (0x01,),
    (0x02, STANDBY): (0x10,), (0x02, RUN): (0x11,), (0x02, SERVICE): (0x91,),
    (0x0F, STANDBY): (0x47,), (0x0F, RUN): (0x47,), (0x0F, SERVICE): (0x47,),
    (0x7E, SERVICE): (0xA5,),
}


class Sensor:
    """An I2C temperature sensor at 0x4B. Its data sheet would give
        a pointer byte, which holds only across a repeated START
        00 temperature (two bytes), 01 control (write 01 run, 00 standby), 02 status, 0F identity 47
    and leave out
        7E    takes a write, and C3 written there while running enters service mode (status 91)
              in service mode 7E reads A5 and a second address, 0x4F, answers with 5E 1D"""

    name = "sensor"
    ADDRESS = 0x4B
    SECOND = 0x4F
    documented = (0x00, 0x01, 0x02, 0x0F)
    hidden = (0x7E, 0xC3)

    def reset(self):
        return (STANDBY, None, ("none", 0))

    def start(self, state, x):
        mode, pointer, _ = state
        if x >> 1 == self.ADDRESS:
            if x & 1:
                at = 0 if pointer is None else pointer
                return (mode, None, (at, 0)), True
            return (mode, None, ("pointer", 0)), True
        if x >> 1 == self.SECOND and mode == SERVICE:
            return (mode, None, ("second", 0) if x & 1 else ("full", 0)), True
        return (mode, None, ("none", 0)), False

    def write(self, state, x):
        mode, pointer, (doing, _) = state
        if doing == "pointer":
            known = x if x in (0x00, 0x01, 0x02, 0x0F, 0x7E) else 0xFF
            return (mode, known, ("data", 0)), True
        if doing == "data":
            if pointer == 0x01 and x in (0x00, 0x01):
                if x == 0:
                    mode = STANDBY
                elif mode == STANDBY:
                    mode = RUN
                return (mode, pointer, ("full", 0)), True
            if pointer == 0x7E:
                if x == 0xC3 and mode == RUN:
                    mode = SERVICE
                return (mode, pointer, ("full", 0)), True
        return (mode, pointer, ("none", 0)), False

    def sends(self, state):
        mode, _, (doing, at) = state
        if doing == "second":
            values = (0x5E, 0x1D)
        elif isinstance(doing, int):
            values = SENSOR_VALUES.get((doing, mode), ())
        else:
            values = ()
        return values[at] if at < len(values) else 0xFF

    def read(self, state, nack):
        mode, pointer, (doing, at) = state
        if nack:
            return (mode, pointer, ("none", 0))
        return (mode, pointer, (doing, min(at + 1, 2)))

    def stop(self, state):
        return (state[0], None, ("none", 0))


class Thermometer:
    """An I2C thermometer at 0x48 with nothing to hide: a pointer that stays, and two registers."""

    name = "thermometer"
    documented = (0x00, 0x01)
    hidden = ()

    def reset(self):
        return (0, ("none", 0))

    def start(self, state, x):
        if x >> 1 != 0x48:
            return (state[0], ("none", 0)), False
        return (state[0], ("read", 0) if x & 1 else ("pointer", 0)), True

    def write(self, state, x):
        if state[1][0] == "pointer" and x in self.documented:
            return (x, ("none", 0)), True
        return (state[0], ("none", 0)), False

    def sends(self, state):
        pointer, (doing, at) = state
        values = () if doing != "read" else (0x60,) if pointer else (0x19, 0x80)
        return values[at] if at < len(values) else 0xFF

    def read(self, state, nack):
        pointer, (doing, at) = state
        return (pointer, ("none", 0) if nack else (doing, min(at + 1, 2)))

    def stop(self, state):
        return (state[0], ("none", 0))


# ---- what the data sheets give, and no more


class FlashAsDocumented(Flash):
    def byte(self, state, x):
        if x in self.hidden and state[2] and state[2][0] == "command":
            return state[:2] + (("ignore", 0),)
        return Flash.byte(self, state, x)


class SensorAsDocumented(Sensor):
    def write(self, state, x):
        if x == self.hidden[0] and state[2][0] == "pointer":
            return (state[0], 0xFF, ("data", 0)), True
        return Sensor.write(self, state, x)


AS_DOCUMENTED = {"flash": FlashAsDocumented, "sensor": SensorAsDocumented, "thermometer": Thermometer}


# ---- the controls


class Lock:
    """SPI: it answers 21, and 4F once the code is clocked in without a break inside one transaction. No step
    on the way shows."""

    def __init__(self, code):
        self.code = tuple(code)
        self.name = "lock of %d steps" % len(self.code)

    def reset(self):
        return None

    def select(self, state):
        return 0

    def deselect(self, state):
        return None

    def byte(self, state, x):
        if state is None or state == len(self.code):
            return state
        return state + 1 if x == self.code[state] else 1 if x == self.code[0] else 0

    def sends(self, state):
        return 0x4F if state == len(self.code) else 0x21


class Store:
    """SPI: 5A x stores any byte and 5B reads it back. A table cannot hold that."""

    name = "data store"

    def reset(self):
        return (0x00, None)

    def select(self, state):
        return (state[0], "command")

    def deselect(self, state):
        return (state[0], None)

    def byte(self, state, x):
        held, doing = state
        if doing == "command":
            return (held, {0x5A: "store", 0x5B: "fetch"}.get(x, "ignore"))
        if doing == "store":
            return (x, "ignore")
        return state

    def sends(self, state):
        return state[0] if state[1] == "fetch" else 0xFF


def lock_code(steps):
    rng = random.Random(steps)
    return [rng.choice((0xA5, 0x5A)) for _ in range(steps)]


LOCK_INPUTS = [0x5A, 0xA5, SELECT]
SILENT_KEY = (0x3C, 0xC3)


def flash():
    return spi_table(Flash())


def sensor():
    return i2c_table(Sensor())


def thermometer():
    return i2c_table(Thermometer())


def lock(steps):
    return spi_table(Lock(lock_code(steps)))


def silent_key():
    table = spi_table(Lock(SILENT_KEY))
    table.name = "silent two-byte key"
    return table


def store():
    return spi_table(Store())


def flaky():
    return flash().but("flash, noisy", flip_ppm=2000, seed=7)


def forgetful():
    return flash().but("flash whose write enable times out", timeout=6000)


def stubborn():
    return flash().but("flash that reset does not clear", keeps_state=1)


def clairvoyant():
    table = flash()
    return table.but("flash that answers the byte still coming in", mask=b"\x01" * table.n)
