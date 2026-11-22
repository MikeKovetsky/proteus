# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The learner and the session, on an ideal link: exact or refused or warned, and never anything else."""

import unittest

from common import Ideal, learn, verdict
from learn.host import bus as bus_module
from learn.host import session
from learn.host.search import Search
from learn.host.spi import Spi
from learn.lab import cloning, devices, judge


class Flat:
    """An SPI device that answers one byte whatever happens."""

    def __init__(self, answer):
        self.answer, self.name = answer, "flat"

    def reset(self):
        return 0

    def select(self, state):
        return 0

    deselect = select

    def byte(self, state, x):
        return 0

    def sends(self, state):
        return self.answer


class Learner(unittest.TestCase):
    def assert_not_failed(self, table, result):
        got = verdict(table, result)
        self.assertNotIn(got, judge.FAILURES, "%s: %s %s" % (table.name, got, result["reason"]))
        if result["ok"]:
            self.assertTrue(result["guarantee"] and result["warnings"] and result["image"])
        else:
            self.assertIsNone(result["image"])
            self.assertTrue(result["reason"])
        return got

    def test_random_devices_are_never_silently_wrong(self):
        exact, total = 0, 0
        for seed in range(24):
            for table in (cloning.scrambled("spi", 2 + seed % 7, seed % 4, seed),
                          cloning.two_byte(2 + seed % 9, seed)):
                total += 1
                exact += self.assert_not_failed(table, learn(table, seed=seed)) == judge.EXACT
        for seed in range(3):
            table = cloning.scrambled("i2c", 2 + seed, 1, seed)
            total += 1
            exact += self.assert_not_failed(table, learn(table, seed=seed)) == judge.EXACT
        self.assertGreaterEqual(exact, total // 2, "only %d of %d exact" % (exact, total))

    def test_the_demonstration_devices_learn_exactly_on_an_ideal_link(self):
        for table, states in ((devices.flash(), 51), (devices.thermometer(), 13)):
            result = learn(table, **({"serve": 2000} if table.bus == "spi" else {}))
            self.assertEqual(self.assert_not_failed(table, result), judge.EXACT)
            self.assertEqual(result["stats"]["states"], states)

    def test_the_demonstration_devices_are_exact_whatever_the_seed(self):
        """The seed orders the questions. The page says the demonstration is no lucky seed, and this is why."""
        for name, told in (("flash", {"serve": 2000}), ("sensor", {}), ("thermometer", {})):
            for seed in range(1, 9):
                table = getattr(devices, name)()
                self.assertEqual(verdict(table, learn(table, seed=seed, **told)), judge.EXACT, (name, seed))

    def test_the_last_sweep_tries_every_symbol_from_every_state(self):
        """The guarantee's first line rests on this: on the pass that found nothing, the sweep put every symbol the
        bus allows to every state of the final model."""
        sweep, test = Search.sweep, Search.test
        sweeps = []

        def recorded_sweep(search, model):
            sweeps.append((model, set()))
            search.tried = sweeps[-1][1]
            try:
                return sweep(search, model)
            finally:
                search.tried = None

        def recorded_test(search, model, q, word, *rest):
            if getattr(search, "tried", None) is not None and word:
                search.tried.add((q, word[0]))
            return test(search, model, q, word, *rest)

        Search.sweep, Search.test = recorded_sweep, recorded_test
        try:
            for table in (devices.flash(), devices.thermometer()):
                del sweeps[:]
                result = learn(table, **({"serve": 2000} if table.bus == "spi" else {}))
                self.assertTrue(result["ok"], result["reason"])
                engine = session.BUSES[table.bus](session.settings({"bus": table.bus}))
                self.assertTrue(sweeps, "no sweep ran")
                model, tried = sweeps[-1]
                self.assertEqual(model.n, result["stats"]["states"])
                missing = [(q, s) for q, phase in enumerate(result["model"]["phases"])
                           for s in range(engine.SYMBOLS) if engine.allows(phase, s) and (q, s) not in tried]
                self.assertEqual(missing, [], table.name)
        finally:
            Search.sweep, Search.test = sweep, test

    def test_a_declared_size_is_not_refused_while_the_model_is_still_small(self):
        table = cloning.two_byte(32, 32)
        inputs = devices.LOCK_INPUTS
        states = judge.Minimal(judge.TRUTH["spi"](table, inputs)).n
        self.assertGreater(states, 20)
        result = learn(table, serve=2000, inputs=inputs, max_states=states)
        self.assertEqual((result["code"], verdict(table, result)), ("ok", judge.EXACT), result["reason"])

    def test_a_declared_lock_is_found(self):
        result = learn(devices.lock(9), serve=2000, inputs=devices.LOCK_INPUTS, max_states=11)
        self.assertEqual((result["code"], verdict(devices.lock(9), result)), ("ok", judge.EXACT))

    def test_without_depth_the_lock_is_missed(self):
        """What finds the lock is the depth suite: take it away and the same device comes out wrong."""
        depth = Search.depth
        Search.depth = lambda self, model: None
        try:
            table = devices.lock(16)
            result = learn(table, serve=2000, inputs=devices.LOCK_INPUTS, max_states=18)
            self.assertNotEqual(verdict(table, result), judge.EXACT)
        finally:
            Search.depth = depth

    def test_a_size_beyond_reach_is_refused(self):
        for steps, declared in ((24, 26), (3, 100)):
            result = learn(devices.lock(steps), inputs=devices.LOCK_INPUTS, max_states=declared)
            self.assertEqual((result["ok"], result["code"]), (False, "bound"), result["reason"])

    def test_a_size_that_is_too_small_is_refused(self):
        result = learn(cloning.two_byte(16, 16), inputs=devices.LOCK_INPUTS, max_states=3)
        self.assertEqual(result["code"], "bound", result["reason"])

    def test_refusals(self):
        flash = devices.flash()
        for code, link, told in (
            ("nondeterministic", Ideal(flash, flip=97), {}),
            ("stuck", Ideal(flash, stuck=True), {}),
            ("budget", Ideal(flash), {"budget": 5000}),
            ("states", Ideal(flash), {"limit_states": 8}),
            ("alphabet", Ideal(flash), {"limit_alphabet": 5}),
            ("too fast", Ideal(flash), {"rate": 2500, "serve": 5000}),
            ("nothing", Ideal(devices.spi_table(Flat(0xFF))), {}),
            ("nothing", Ideal(devices.spi_table(Flat(0x00))), {}),
        ):
            result = session.run(dict(told, bus="spi"), link)
            self.assertEqual((result["ok"], result["code"], result["image"]), (False, code, None), result["reason"])

    def test_a_machine_too_large_for_the_table_is_refused(self):
        table = cloning.two_byte(128, 128)
        made, result = cloning.generate(table, 2000)
        self.assertGreater(made["entries"], 512)
        self.assertIsNone(made["image"])
        self.assertFalse(result["ok"])

    def test_what_is_told_is_checked(self):
        link = Ideal(devices.flash())
        for told in ({}, {"bus": "uart"}, {"bus": "spi", "truth": [1]}, {"bus": "spi", "rate": 3000},
                     {"bus": "spi", "reset_us": 0}, {"bus": "spi", "extra": 33}, {"bus": "spi", "inputs": []},
                     {"bus": "spi", "seed": True}, {"bus": "spi", "boot_us": 256}, {"bus": "spi", "inputs": [300]}):
            result = session.run(told, link)
            self.assertEqual((result["ok"], result["code"]), (False, "told"), told)
        self.assertEqual(link.images, [])

    def test_what_had_to_be_told_is_reported(self):
        table = cloning.two_byte(4, 4)
        result = session.run({"bus": "spi", "serve": 2000, "max_states": 9}, Ideal(table))
        self.assertTrue(result["ok"], result["reason"])
        needed = " ".join(result["needs"])
        for part in ("P0 SCK", "2500 kHz", "2000 kHz", "P4", "reset", "most states the device has: 9"):
            self.assertIn(part, needed)
        warned = " ".join(result["warnings"])
        self.assertIn("tested and not proven", warned)
        self.assertIn("Nothing was asked at that rate", warned)

    def test_the_host_crashing_is_a_refusal_too(self):
        class Dead(Ideal):
            def exchange(self, data, wanted):
                raise OSError("unplugged")

        result = session.run({"bus": "spi"}, Dead(devices.flash()))
        self.assertEqual((result["ok"], result["code"], result["image"]), (False, "error", None))

    def test_queries(self):
        bus = Spi(session.settings({"bus": "spi"}))
        self.assertEqual(bus.encode((256, 0x9F, 257)), bytes((0, 1, 5, 1, 3, 0x9F, 2)))
        paused = bus.encode((256,), (0,), 25 * 300)
        self.assertEqual(paused, bytes((0, 1, 5, 4, 255, 4, 45, 1)))
        self.assertEqual(bus.decode((257, 256, 7), b"\x00\x00\x33"), (-1, -1, -1))
        self.assertEqual(bus.decode((256, 7, 257), b"\x00\x33\x00"), (0, 0x33, 0))
        self.assertEqual(bus_module.PAUSE_US, 25)


if __name__ == "__main__":
    unittest.main()
