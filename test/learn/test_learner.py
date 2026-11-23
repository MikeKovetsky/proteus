# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The learner and the session, on an ideal link: exact or refused or warned, and never anything else."""

import unittest

from common import Ideal, learn, verdict
from learn.host import bus as bus_module
from learn.host import learner as learner_module
from learn.host import session
from learn.host.search import Search
from learn.host.spi import Spi
from learn.lab import cloning, devices, families, judge


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

    def missing_from_sweep(self, engine, search, model, tried):
        """The Wp tests with no extra state that `tried` lacks: from every state its phase's W, and every symbol
        the bus allows there followed by each word of W for the state it leads to. The symbols come from the
        bus's grammar, not from the search's own list of them."""
        missing = []
        for q in range(model.n):
            phase = model.phases[q]
            for word in search.whole[phase]:
                if (q, tuple(word)) not in tried:
                    missing.append((q, tuple(word)))
            for s in range(engine.SYMBOLS):
                if engine.allows(phase, s):
                    for word in search.identify[model.step(q, s)[0]]:
                        if (q, (s,) + tuple(word)) not in tried:
                            missing.append((q, (s,) + tuple(word)))
        return missing

    def test_the_last_sweep_tries_every_symbol_from_every_state(self):
        """The guarantee's first line rests on this: on the pass that found nothing, the sweep put every symbol the
        bus allows to every state of the final model, each followed by every word that identifies where it
        leads. Whole tests are recorded, not their first symbols, since a first symbol can come from another
        test that proves nothing about it. Here both last sweeps start at state 0's first symbol, so the next
        test is the one that starts a sweep inside a state."""
        sweep, test = Search.sweep, Search.test
        sweeps = []

        def recorded_sweep(search, model):
            sweeps.append((search, model, set(), search.sweep_from))
            search.tried = sweeps[-1][2]
            try:
                return sweep(search, model)
            finally:
                search.tried = None

        def recorded_test(search, model, q, word, *rest):
            if getattr(search, "tried", None) is not None:
                search.tried.add((q, tuple(word)))
            return test(search, model, q, word, *rest)

        Search.sweep, Search.test = recorded_sweep, recorded_test
        try:
            for table in (devices.flash(), devices.thermometer()):
                del sweeps[:]
                result = learn(table, **({"serve": 2000} if table.bus == "spi" else {}))
                self.assertTrue(result["ok"], result["reason"])
                engine = session.BUSES[table.bus](session.settings({"bus": table.bus}))
                self.assertTrue(sweeps, "no sweep ran")
                search, model, tried, _ = sweeps[-1]
                self.assertEqual(model.n, result["stats"]["states"])
                self.assertEqual(self.missing_from_sweep(engine, search, model, tried), [], table.name)
        finally:
            Search.sweep, Search.test = sweep, test

    def test_a_sweep_that_starts_inside_a_state_still_covers_it(self):
        """A sweep resumes where the last counterexample was found, inside some state's symbols, and has to come
        round to that state again for the symbols before it. Started at chosen places on a model that is already
        exact, it must find nothing and leave no test out."""
        for table in (devices.thermometer(), cloning.two_byte(6, 3)):
            told = session.settings({"bus": table.bus})
            engine = session.BUSES[table.bus](told)
            learner = learner_module.Learner(engine, Ideal(table), told)
            search = Search(learner)
            model = learner.learn(search)
            self.assertEqual(verdict(table, {"ok": True, "told": told, "model": session.exported(model, engine),
                                             "stats": {"extra": search.extra}, "warnings": ["w"]}), judge.EXACT)
            search.prepare(model)
            widest = max(range(model.n), key=lambda q: engine.count(model.phases[q]))
            starts = [(widest, 1), (widest, engine.count(model.phases[widest]) - 1),
                      (model.n - 1, engine.count(model.phases[model.n - 1]) // 2), (0, 0)]
            for first, at in starts:
                tried = set()

                def recorded(model_, q, word, *rest):
                    tried.add((q, tuple(word)))
                    return Search.test(search, model_, q, word, *rest)

                search.test = recorded
                search.sweep_from = (first, at)
                try:
                    self.assertIsNone(search.sweep(model), (table.name, first, at))
                finally:
                    del search.test
                self.assertEqual(self.missing_from_sweep(engine, search, model, tried), [],
                                 "%s, from state %d at %d" % (table.name, first, at))

    def test_a_bank_switch_is_found_by_the_pairs_search(self):
        """A command and then a value that show nothing until a later read: the suites take the command for the
        default, so only the pairs search tries it with every value after it. Without it the copy is wrong."""
        banks = [i for i in range(16) if families.FAMILIES["spi registers"](
            families.random.Random(families.seed("spi registers", i))).banks > 1][:2]
        self.assertEqual(len(banks), 2)
        pairs = Search.pairs
        for i in banks:
            table = families.device("spi registers", i)
            result = learn(table)
            self.assertEqual(verdict(table, result), judge.EXACT, table.name)
            self.assertGreater(result["stats"]["found_by"].get("pairs", 0), 0, table.name)
            self.assertIn("found outside the bounds", result["flags"])
            Search.pairs = lambda self, model: None
            try:
                self.assertEqual(verdict(table, learn(table)), judge.MISSED, table.name)
            finally:
                Search.pairs = pairs

    def test_the_pairs_search_covers_every_opening_from_every_mode(self):
        """On the pass that found nothing, every two-byte opening, from every state that waits for a transaction,
        with what reads its effect back."""
        test = Search.test
        passes = []

        def recorded(search, model, q, word, *rest):
            if getattr(search, "in_pairs", False):
                passes[-1][1].add(q)
                for k in range(0, len(word), 5):
                    if word[k] == 256 and word[k + 4] == 257:
                        passes[-1][2].add((q, word[k + 1], word[k + 2]))
            return test(search, model, q, word, *rest)

        pairs = Search.pairs

        def recorded_pairs(search, model):
            passes.append((model, set(), set()))
            search.in_pairs = True
            try:
                return pairs(search, model)
            finally:
                search.in_pairs = False

        Search.test, Search.pairs = recorded, recorded_pairs
        try:
            table = families.device("spi registers", 1)
            result = learn(table)
        finally:
            Search.test, Search.pairs = test, pairs
        self.assertEqual(verdict(table, result), judge.EXACT)
        model, asked, opened = passes[-1]
        modes = [q for q in range(len(model.rows)) if model.phases[q] == 0]
        self.assertEqual(asked, set(modes))
        self.assertEqual(len(opened), len(modes) * 256 * 256)  # a read-back is an opening too, and one of these
        self.assertEqual(tuple(result["stats"]["pairs"]), (len(modes) * 256 * 256 // 128,) * 2)

    def test_a_pairs_search_cut_short_is_flagged(self):
        whole = learn(devices.flash(), serve=2000)
        before = whole["stats"]["symbols"]
        self.assertEqual(whole["flags"], [])
        result = learn(devices.flash(), serve=2000, budget=before * 2 // 3)
        self.assertTrue(result["ok"], result["reason"])
        asked, of = result["stats"]["pairs"]
        self.assertLess(asked, of)
        self.assertIn("pairs cut short", result["flags"])
        self.assertIn("%d of %d questions" % (asked, of), " ".join(result["warnings"]))
        self.assertNotIn("every two bytes were tried", " ".join(result["warnings"]))
        self.assertIn("every two bytes were tried", " ".join(whole["warnings"]))

    def test_an_untraced_secret_leaves_no_trace_in_the_result(self):
        """Why no flag can single out an untraced secret: the run of a device whose key is never completed is the
        run of its twin, whose key can't be, and the twin's copy is exact. Every line the host prints is the
        same for both."""
        for family in ("spi untraced", "i2c untraced"):
            for i in range(2):
                part, twin = (families.FAMILIES[family](families.random.Random(families.seed(family, i)))
                              for _ in range(2))
                twin.key = twin.key + [256]  # no byte is 256, so the key never opens
                make = devices.spi_table if part.name == "spi" else devices.i2c_table
                tables = [make(part), make(twin)]
                results = [learn(table) for table in tables]
                self.assertEqual([verdict(t, r) for t, r in zip(tables, results)], [judge.MISSED, judge.EXACT],
                                 (family, i))
                for key in ("model", "image", "flags", "warnings", "guarantee"):
                    self.assertEqual(results[0][key], results[1][key], (family, i, key))

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
            ("too fast", Ideal(flash), {"rate": 2500, "serve": 10000}),
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
