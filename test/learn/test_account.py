# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The readable account of a learned device, checked against what the devices' own descriptions say
(learn/lab/devices.py), which the account never sees."""

import re
import unittest

from common import learn
from learn import account
from learn.lab import cloning, devices, families


def described(table, inputs=None):
    """The model of the device's own minimal machine, as the host exports a model."""
    return cloning.generate(table, 400 if table.bus == "i2c" else 1000, inputs)[1]["model"]


def modes(lines):
    """{mode number: (how it is reached, its lines)}."""
    out, now = {}, None
    for line in lines:
        found = re.match(r"Mode (\d+), reached by (.*)\.$", line)
        if found:
            now = int(found.group(1))
            out[now] = (found.group(2), [])
        elif now is not None and line:
            out[now][1].append(line)
    return out


def line_for(lines, first):
    hits = [line.strip() for line in lines if line.strip().startswith(first + " ")]
    return hits[0] if hits else ""


class Account(unittest.TestCase):
    def test_the_learned_flash_reads_as_its_data_sheet(self):
        table = devices.flash()
        result = learn(table, serve=2000)
        self.assertTrue(result["ok"], result["reason"])
        lines = account.readable("spi", result["model"])
        # the learned model has 13 symbols and the defaults, the described one every byte: they read the same
        self.assertLess(len(result["model"]["alphabet"]), 20)
        self.assertEqual(lines, account.readable("spi", described(table)))
        found = modes(lines)
        ready = found[0][1]
        self.assertEqual(found[0][0], "reset")
        self.assertEqual(ready[0].strip(), "CS falls. MISO sends FF during the first byte, which does this:")
        self.assertIn("sends %s, then 00 repeated" % account.hexed(devices.FLASH_ID), line_for(ready, "9F"))
        self.assertIn("sends 00 repeated; CS rises: mode 0", line_for(ready, "05"))
        self.assertIn("any other", ready[-1])
        self.assertNotIn("5E", " ".join(ready))  # outside write enable the test mode's command does nothing
        # write enable takes effect at the deselect, and only as a one-byte transaction
        enable = re.search(r"right after it, mode (\d+); after 1 or more bytes, mode 0", line_for(ready, "06"))
        self.assertTrue(enable, line_for(ready, "06"))
        enabled = found[int(enable.group(1))]
        self.assertEqual(enabled[0], "reset, then the transaction 06")
        self.assertIn("sends 02 repeated", line_for(enabled[1], "05"))
        # the hidden test mode, and the key that only it reads
        test_mode = re.search(r"right after it, mode (\d+)", line_for(enabled[1], "5E"))
        self.assertTrue(test_mode, line_for(enabled[1], "5E"))
        hidden = found[int(test_mode.group(1))]
        self.assertEqual(hidden[0], "reset, then the transactions 06 | 5E")
        self.assertIn("sends 80 repeated", line_for(hidden[1], "05"))
        self.assertIn("sends %s, then 00 repeated" % account.hexed(devices.FLASH_KEY), line_for(hidden[1], "E7"))
        self.assertIn("sends %s 2C, then 00 repeated" % account.hexed(devices.FLASH_ID), line_for(hidden[1], "9F"))
        # asleep, it answers only the wake-up
        asleep = found[int(re.search(r"right after it, mode (\d+)", line_for(ready, "B9")).group(1))][1]
        self.assertEqual([line_for(asleep, "AB")[:2], asleep[-1].split()[0:2]], ["AB", ["any", "other"]])
        self.assertEqual(len(asleep), 3)

    def test_the_sensor_shows_its_registers_and_its_hidden_address(self):
        lines = account.readable("i2c", described(devices.sensor()))
        found = modes(lines)
        standby = "\n".join(found[0][1])
        for pointer, values in ((0x00, "80 00"), (0x02, "10"), (0x0F, "47")):
            self.assertRegex(standby, r"%02X +ACK; STOP: mode 0; any more bytes: NACK\n +repeated START 4B\+R: "
                                      r"ACK, then reads %s, then FF repeated" % (pointer, values))
        self.assertIn("START at any other address: NACK", standby)
        self.assertNotIn("4F", standby)
        service = [number for number, (how, _) in found.items() if "7E C3" in how]
        self.assertEqual(len(service), 1, found.keys())
        self.assertEqual(found[service[0]][0], "reset, then START 4B+W 01 01 START 4B+W 7E C3 STOP")
        text = "\n".join(found[service[0]][1])
        self.assertIn("START 4F+R: ACK, then reads 5E 1D, then FF repeated", text)
        self.assertRegex(text, r"02 +ACK; STOP: mode %d; any more bytes: NACK\n +repeated START 4B\+R: ACK, then "
                               r"reads 91" % service[0])

    def test_the_thermometer_has_two_modes_and_nothing_hidden(self):
        lines = account.readable("i2c", described(devices.thermometer()))
        found = modes(lines)
        self.assertEqual(sorted(found), [0, 1])
        self.assertIn("START 48+R: ACK, then reads 19 80, then FF repeated", "\n".join(found[0][1]))
        self.assertIn("START 48+R: ACK, then reads 60, then FF repeated", "\n".join(found[1][1]))

    def test_a_command_with_an_argument_shows_the_argument(self):
        family = families.FAMILIES["spi registers"]
        for i in range(16):
            part = family(families.random.Random(families.seed("spi registers", i)))
            if part.banks > 1:
                break
        table = families.device("spi registers", i)
        lines = account.readable("spi", described(table))
        switch = line_for(modes(lines)[0][1], "%02X" % part.switch)
        self.assertIn("the next byte matters", switch)
        picks = [line for line in lines if re.match(r" +%02X +sends" % part.picks[1], line)]
        self.assertTrue(picks and "CS rises: mode" in picks[0], lines)

    def test_a_lock_is_shown_as_its_states(self):
        model = described(devices.lock(5), devices.LOCK_INPUTS)
        lines = account.readable("spi", model)
        self.assertIn("shown as its states", lines[0])
        self.assertEqual(len(lines), 1 + len(model["rows"]))
        self.assertIn("sends 4F", lines[-1])

    def test_each_mode_is_reached_the_shortest_way(self):
        tables = [(devices.flash(), None), (devices.sensor(), None), (devices.lock(5), devices.LOCK_INPUTS)]
        tables += [(cloning.scrambled(bus, 6, 2, seed), None) for seed in range(12) for bus in ("spi", "i2c")]
        for table, inputs in tables:
            machine = account.Machine(table.bus, described(table, inputs))
            far, todo = {0: 0}, [0]
            for q in todo:
                for s in machine.symbols(q):
                    found = machine.step(q, s)
                    if found and found[0] not in far:
                        far[found[0]] = far[q] + 1
                        todo.append(found[0])
            mapper = account.Mapper(machine)
            self.assertEqual(sorted(mapper.modes), sorted(q for q in far if machine.phases[q] == 0))
            for q in mapper.modes:
                self.assertEqual(len(mapper.reach[q]), far[q], (table.name, q))
                at = 0
                for s in mapper.reach[q]:
                    at = machine.step(at, s)[0]
                self.assertEqual(at, q)

    def test_a_byte_not_learned_acts_as_its_default(self):
        model = described(devices.flash())
        machine = account.Machine("spi", model)
        default = model["defaults"][0]
        self.assertNotIn(0x42, model["alphabet"])
        for q in range(machine.n):
            if machine.phases[q] == 1:
                self.assertEqual(machine.step(q, 0x42), machine.step(q, default))
                self.assertIsNotNone(machine.step(q, 0x42))

    def test_a_stream_is_written_once(self):
        self.assertEqual(account.stream([0xFF, 0xFF], 1, "sends"), "sends FF repeated")
        self.assertEqual(account.stream([0x4D, 0x00, 0x00], 2, "sends"), "sends 4D, then 00 repeated")
        self.assertEqual(account.stream([1, 2, 3], 1, "reads"), "reads 01, then 02 03 over and over")
        self.assertEqual(account.stream([7], None, "sends"), "sends 07 ...")

    def test_the_graph_has_every_state_and_edge(self):
        model = described(devices.thermometer())
        lines = account.dot("i2c", model, "thermometer")
        self.assertEqual((lines[0], lines[-1]), ("digraph thermometer {", "}"))
        nodes = [line for line in lines if re.match(r"  s\d+ \[", line)]
        edges = [line for line in lines if " -> " in line]
        self.assertEqual(len(nodes), len(model["rows"]))
        pairs = set()
        machine = account.Machine("i2c", model)
        for q in range(machine.n):
            for s in machine.symbols(q):
                found = machine.step(q, s)
                if found:
                    pairs.add((q, found[0]))
        self.assertEqual(len(edges), len(pairs))
        self.assertEqual(sum(line.count("[") for line in lines), sum(line.count("]") for line in lines))


if __name__ == "__main__":
    unittest.main()
