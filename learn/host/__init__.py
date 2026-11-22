# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The host's half of learn-and-become. The host asks, the chip puts each question on the pins and returns the
answer, and from those answers alone the host writes the program that makes the chip behave as the device did.

    learner.py   the classification-tree learner (Angluin's L* family, Rivest-Schapire counterexamples)
    search.py    the equivalence tests and what they guarantee
    model.py     the learned Mealy machine and its distinguishing sequences
    bus.py       what every bus adapter provides
    spi.py i2c.py the adapters: symbols to the operations of the chip's query engines
    table.py     table generation: shared nodes packed by the chip's base | cA | cB << 1 addressing
    clone_spi.py clone_i2c.py  the table of a learned SPI or I2C target
    session.py   one run: from what the host is told and a link to the chip, to a program or a refusal

Nothing here imports anything outside this package, opens a file or has a third-party dependency. It is written
in the subset of Python that MicroPython has. It has not been run under MicroPython.
"""
