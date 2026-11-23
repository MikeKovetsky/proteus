# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Bit-level shells round the devices' behaviour, for the Icarus bench. They share no code with board.cpp's
shells, and test/learn holds the two to the same answers."""

from cocotb.triggers import Timer

from bench import PS_PER_NS

SCK, MOSI, MISO, CS = 0, 1, 2, 3
SCL, SDA = 0, 1
NRESET = 4
SPI_VALID = 50.0  # ns from the edge to the bit
I2C_VALID = 340.0


class Shell:
    def __init__(self, lines, part):
        self.lines, self.part = lines, part
        self.state = part.reset()
        self.token = 0
        self.clear()
        lines.listen(self.changed)

    def put(self, pin, level, after):
        self.token += 1
        token = self.token

        async def later():
            await Timer(round(after * PS_PER_NS), unit="ps")
            if token == self.token:
                self.lines.drive(pin, level)

        self.lines.soon(later())

    def changed(self, changed, level):
        if not level[NRESET]:
            if NRESET in changed:
                self.state = self.part.reset()
                self.clear()
                self.put(self.PIN, None, 1)
            return
        self.edge(changed, level)


class SpiDevice(Shell):
    PIN = MISO

    def clear(self):
        self.selected = False
        self.taken, self.giving = [], []

    def edge(self, changed, level):
        part = self.part
        if CS in changed:
            self.selected = changed[CS] == 0
            if self.selected:
                self.state = part.select(self.state)
                self.taken, self.giving = [], self.bits()
                self.put(MISO, self.giving.pop(0), SPI_VALID)
            else:
                self.state = part.deselect(self.state)
                self.put(MISO, None, SPI_VALID)
        elif self.selected and SCK in changed:
            if changed[SCK]:
                self.taken.append(level[MOSI])
                if len(self.taken) == 8:
                    self.state = part.byte(self.state, int("".join(map(str, self.taken)), 2))
                    self.taken, self.giving = [], self.bits()
            elif self.giving:
                self.put(MISO, self.giving.pop(0), SPI_VALID)

    def bits(self):
        return [int(bit) for bit in format(self.part.sends(self.state), "08b")]


class I2cDevice(Shell):
    PIN = SDA

    def clear(self):
        self.role, self.busy = None, False
        self.got, self.sampled, self.acked = [], None, False
        self.out = 0

    def release(self):
        self.put(SDA, None, I2C_VALID)

    def edge(self, changed, level):
        if SDA in changed and SCL not in changed and level[SCL]:
            self.got, self.sampled = [], None
            if changed[SDA]:
                if self.busy:
                    self.state = self.part.stop(self.state)
                self.role, self.busy = None, False
            else:
                self.role, self.busy = "address", True
            self.put(SDA, None, 1)
        elif SCL in changed:
            if changed[SCL]:
                self.sampled = level[SDA]
            elif self.sampled is not None:
                self.got.append(self.sampled)
                self.sampled = None
                self.fallen()

    def fallen(self):
        count, part = len(self.got), self.part
        if count == 8:
            value = int("".join(map(str, self.got)), 2)
            self.acked = False
            if self.role == "address":
                self.state, self.acked = part.start(self.state, value)
            elif self.role == "write":
                self.state, self.acked = part.write(self.state, value)
            if self.acked:
                self.put(SDA, 0, I2C_VALID)
            else:
                self.release()
        elif count == 9:
            first, last = self.got[7], self.got[8]
            self.got = []
            if self.role == "address":
                self.role = ("read" if first else "write") if self.acked else None
            elif self.role == "write" and not self.acked:
                self.role = None
            elif self.role == "read":
                self.state = part.read(self.state, bool(last))
                if last:
                    self.role = None
            if self.role == "read":
                self.out = part.sends(self.state)
                self.give(7)
            else:
                self.release()
        elif self.role == "read":
            self.give(7 - count)

    def give(self, bit):
        self.put(SDA, None if self.out >> bit & 1 else 0, I2C_VALID)


SHELL = {"spi": SpiDevice, "i2c": I2cDevice}
