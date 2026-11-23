# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The table generators, on given machines: the clone on RTL against the truth, every symbol from every
reachable pair. And the judge catches each planted fault."""

import unittest

import common  # noqa: F401
from learn.host import clone_i2c, clone_spi
from learn.host import table as T
from learn.host.bus import Refused
from learn.lab import board, cloning, devices, families, judge


class Fan:
    """k addresses and k data bytes act unlike the rest. With `split`, a read is ACKed at any address."""

    name = "fan"

    def __init__(self, k, split):
        self.k, self.split = k, split

    def reset(self):
        return (0, 0)

    def start(self, state, x):
        hit = x >> 1 < self.k and not x & 1
        ack = hit or bool(self.split and x & 1)
        return (1 if hit else 2 if ack else 0, (x >> 1) % 3 if hit else 0), ack

    def write(self, state, x):
        return ((1, x % 2) if x < self.k else (0, 0)), x < self.k

    def sends(self, state):
        return 0x35

    def read(self, state, nack):
        return (0, 0) if nack else state

    def stop(self, state):
        return (0, 0)


def judged(table, serve):
    made, result = cloning.generate(table, serve)
    if not result["ok"]:
        return made, None
    with board.Board(table.bus, 3) as on:
        return made, judge.judge(on, table, result, [serve])


class Generators(unittest.TestCase):
    def test_random_machines(self):
        exact = 0
        cases = [("spi", 2 + seed % 30, seed % 9, seed, (2000, 1000, 500)[seed % 3]) for seed in range(40)]
        cases += [("i2c", 2 + seed % 8, seed % 5, seed, (400, 100)[seed % 2]) for seed in range(12)]
        for bus, states, special, seed, serve in cases:
            table = cloning.scrambled(bus, states, special, seed)
            try:
                _, report = judged(table, serve)
            except Refused:
                continue
            if report:
                self.assertEqual(report["verdict"], judge.EXACT, "%s at %d kHz: %s" % (table.name, serve, report))
                exact += 1
        self.assertGreater(exact, 35)

    def test_at_the_spi_timing_bound(self):
        """No decision deeper than the rate allows is written, and what is written is exact at that rate. At 6000
        kHz, 10 clocks a bit, a decision may take three tests; where a state needs more, the copy is refused."""
        tried, refused = 0, 0
        for seed in range(8):
            table = cloning.scrambled("spi", 2 + seed % 3, 2 + 3 * seed, seed)
            try:
                made, report = judged(table, 6000)
            except Refused as refusal:
                self.assertEqual(refusal.code, "too fast")
                refused += 1
                continue
            self.assertLessEqual(clone_spi.period(made["chain"]), 10)
            self.assertEqual(report["verdict"], judge.EXACT, "%s, depth %d" % (table.name, made["chain"]))
            tried += 1
        self.assertGreater(tried, 2)
        self.assertGreater(refused, 0)

    def test_the_spi_timing_bound_is_what_the_rtl_needs(self):
        """Without its margin, the bound is exact for a controller on the chip's own clock, as the judge's is, and
        one clock faster the judge finds a fault: AFTER and LOW are what the RTL takes, and MARGIN is spare. The
        first device's limit is its decision, the second's is SCK's low half."""
        for table, depth in ((cloning.scrambled("spi", 2, 20, 19), 4), (families.device("spi modes", 6), 1)):
            made, result = cloning.generate(table, 5000)
            self.assertEqual(made["chain"], depth, table.name)
            least = max(clone_spi.AFTER + depth, 2 * clone_spi.LOW)
            self.assertEqual(clone_spi.period(depth), max(least + clone_spi.MARGIN, 2 * (clone_spi.LOW + 1)))
            minimal = judge.Minimal(judge.TRUTH["spi"](table))
            with board.Board("spi", 3) as on:
                fine = judge.clone(on, result["image"], minimal, 60000 // least)
                late = judge.clone(on, result["image"], minimal, 60000 // (least - 1))
            self.assertTrue(fine["equal"] and fine["complete"], (table.name, least, fine))
            self.assertFalse(late["equal"], (table.name, least - 1))
            self.assertEqual(late["fault"], "MISO moves while SCK is high")

    def test_at_the_i2c_timing_bound(self):
        for k, split, serve in ((38, 0, 400), (29, 1, 400), (100, 0, 100)):
            made, report = judged(devices.i2c_table(Fan(k, split)), serve)
            self.assertEqual(report["verdict"], judge.EXACT, (k, split, serve, report))
            self.assertEqual(made["chain"], k)
        for k, split in ((39, 0), (30, 1)):
            with self.assertRaises(Refused):
                cloning.generate(devices.i2c_table(Fan(k, split)), 400)

    def test_planted_faults_are_caught(self):
        for module, name, wrong, bus in (
            (clone_spi, "K7", 0x600, "spi"), (clone_spi, "J1", 0, "spi"), (T, "EQUAL", T.EDGE, "spi"),
            (T, "DIN_B", T.DIN_A, "spi"), (T, "DATA", T.NDATA, "spi"), (T, "RELEASE", 0, "spi"),
            (clone_spi, "NRESET", T.TX1, "spi"),  # deaf to nRESET: only the judge's reset symbol finds it
            (clone_i2c, "K7", 0x600, "i2c"), (clone_i2c, "K8", 0x900, "i2c"), (clone_i2c, "K1", 0, "i2c"),
            (clone_i2c, "HOLD", 90, "i2c"), (T, "OPEN_DRAIN", T.DATA, "i2c"), (T, "DIN_B", T.DIN_A, "i2c"),
            (clone_i2c, "NRESET", T.TX1, "i2c"),  # deaf to nRESET: only the judge's reset symbol finds it
        ):
            table = devices.flash() if bus == "spi" else devices.thermometer()
            right = getattr(module, name)
            setattr(module, name, wrong)
            try:
                _, report = judged(table, 2000 if bus == "spi" else 400)
            finally:
                setattr(module, name, right)
            self.assertEqual(report["verdict"], judge.WRONG_CLONE, "%s.%s was not caught" % (module.__name__, name))

    def test_the_unbroken_generators_pass_the_same_check(self):
        for table, serve in ((devices.flash(), 2000), (devices.thermometer(), 400), (devices.sensor(), 400)):
            self.assertEqual(judged(table, serve)[1]["verdict"], judge.EXACT, table.name)

    def test_the_first_probes_own_two_largest_machines_fit(self):
        for raw, entries in ((64, 316), (100, 480)):
            table = cloning.probes_machine(raw)
            made, result = cloning.generate(table, 2000, [0x5A, 0xA5, judge.SELECT])
            self.assertEqual(made["entries"], entries)
            with board.Board("spi") as on:
                self.assertEqual(judge.judge(on, table, result, [2000])["verdict"], judge.EXACT)

    def test_v2_flash_serves_at_5mhz_and_6mhz_with_tcam(self):
        """With RTL v2's 1-clock TCAM dispatch and P4 nRESET, Flash serves at 5 MHz and 6 MHz."""
        for serve in (5000, 6000):
            made, report = judged(devices.flash(), serve)
            self.assertEqual(report["verdict"], judge.EXACT, (serve, report))
            self.assertLessEqual(made["entries"], T.DEPTH)
            self.assertTrue(made["image"][0] & 0x80, "expected v2 TCAM header at %d kHz" % serve)

    def test_the_larger_sizes_of_the_first_probe_fit(self):
        for raw, states in ((64, 52), (100, 86)):
            table = cloning.two_byte(raw, raw)
            made, result = cloning.generate(table, 2000, devices.LOCK_INPUTS)
            with board.Board("spi") as on:
                report = judge.judge(on, table, result)
            self.assertEqual((report["true_states"], report["verdict"]), (states, judge.EXACT))
            self.assertLess(made["entries"], 5.3 * states)


if __name__ == "__main__":
    unittest.main()
