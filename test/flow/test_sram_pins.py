# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The SRAM pin buffers and diodes of librelane_plugin_protoemu, and the config they rely on."""
import contextlib
import io
import json
import os
import re
import types
import unittest

import fakes

buffers, BUFFER_OPTIONS = fakes.load_script("odb_sram_buffers")
diodes, DIODE_OPTIONS = fakes.load_script("odb_sram_diodes")
plugin, STEPS = fakes.load_plugin()

BUF = fakes.Master("sg13cmos5l_buf_2", [("A", "input"), ("X", "output"), ("VDD", "power"), ("VSS", "power")])
LIVE = {"A_MEN", "A_WEN", "A_REN"}
LIVE |= {"A_ADDR[%d]" % i for i in range(9)}
LIVE |= {"%s[%d]" % (bus, i) for bus in ("A_DIN", "A_BM", "A_DOUT") for i in range(32)}


def no_duplicate_keys(pairs):
    keys = [key for key, _ in pairs if key != "//"]
    assert len(keys) == len(set(keys)), "duplicate key among %s" % keys
    return dict(pairs)


def config():
    with open(os.path.join(fakes.ROOT, "src", "config.json"), encoding="utf8") as f:
        return json.load(f, object_pairs_hook=no_duplicate_keys)


def srams(block):
    return [i for i in block.getInsts() if i.master.isBlock()]


def connectivity(block):
    """Each input terminal of the original design, and the original output that drives it through any buffers."""
    result = {}
    for inst in block.getInsts():
        if inst.master is BUF:
            continue
        for term in inst.terms:
            if term.direction != "input" or term.net is None:
                continue
            at = term
            for _ in range(3):  # at most a buffer at each end of an SRAM-to-SRAM wire; more is a loop
                sources = [t for t in at.net.terms if t.direction == "output"]
                assert len(sources) == 1, "%d drivers on %s" % (len(sources), at.net.name)
                if sources[0].inst.master is not BUF:
                    break
                at = sources[0].inst.pin("A")
            else:
                raise AssertionError("a loop of buffers before %s/%s" % (inst.name, term.port))
            result[(inst.name, term.port)] = (sources[0].inst.name, sources[0].port)
    return result


def buffered():
    block = fakes.table_design()
    count = buffers.add_buffers(block, BUF, plugin.MACRO_PREFIX, plugin.PIN_NET_PREFIX)
    return block, count


class Buffers(unittest.TestCase):
    def test_216_buffers_keep_every_connection(self):
        before = connectivity(fakes.table_design())
        block, count = buffered()
        self.assertEqual(count, 216)
        self.assertEqual(sum(i.master is BUF for i in block.getInsts()), 216)
        self.assertEqual(connectivity(block), before)

    def test_each_live_pin_sees_one_buffer_and_nothing_else(self):
        block, _ = buffered()
        for macro in srams(block):
            seen = set()
            for pin in macro.terms:
                if pin.port not in LIVE:
                    continue
                others = [t for t in pin.net.terms if t is not pin]
                self.assertEqual(len(others), 1, pin.port)
                self.assertIs(others[0].inst.master, BUF)
                self.assertEqual(others[0].port, "A" if pin.direction == "output" else "X", pin.port)
                seen.add(pin.port)
            self.assertEqual(seen, LIVE)

    def test_clock_and_tied_pins_keep_their_nets(self):
        before = {(m.name, p.port): p.net.name for m in srams(fakes.table_design()) for p in m.terms
                  if p.port not in LIVE and p.net is not None}
        block, _ = buffered()
        after = {(m.name, p.port): p.net.name for m in srams(block) for p in m.terms
                 if p.port not in LIVE and p.net is not None}
        self.assertEqual(after, before)
        self.assertIn(("core.table_ram.ram_a", "A_CLK"), after)

    def test_dont_touch_matches_the_short_nets_only(self):
        rx = re.compile(config()["RSZ_DONT_TOUCH_RX"])
        original = set(fakes.table_design().nets)
        block, _ = buffered()
        self.assertEqual({n for n in block.nets if rx.search(n)}, set(block.nets) - original)
        self.assertEqual(len(set(block.nets) - original), 216)
        # A frozen instance makes OpenROAD's hold repair fail with RSZ-3009 (RepairHold::makeHoldDelay).
        self.assertEqual([i for i in block.insts if rx.search(i)], [])
        for other in ("hold1", "net12", "ANTENNA_sram_pin_buf_ram_a_A_DIN_3_A", "clknet_0_clk"):
            self.assertIsNone(rx.search(other), other)

    def test_new_names_need_no_escaping(self):
        original = fakes.table_design()
        block, _ = buffered()
        for name in (set(block.nets) - set(original.nets)) | (set(block.insts) - set(original.insts)):
            self.assertRegex(name, r"^[A-Za-z_][A-Za-z0-9_]*$")
        self.assertIn("sram_pin_net_ram_b_A_DIN_31", block.nets)

    def test_each_buffer_is_in_a_row_beside_its_pin(self):
        block, _ = buffered()
        for macro in srams(block):
            for pin in macro.terms:
                if pin.port not in LIVE:
                    continue
                buf = next(t.inst for t in pin.net.terms if t is not pin)
                x, y = buf.location
                row = next(r for r in block.rows if r.box.yMin() == y and r.box.xMin() <= x
                           and x + BUF.width <= r.box.xMax())
                self.assertEqual(buf.orient, row.orient)
                self.assertEqual(buf.status, "PLACED")
                # The rows' edge is within one row of either pin edge.
                self.assertLessEqual(abs(x + BUF.width // 2 - pin.at[0]), 1200, pin.port)
                self.assertLessEqual(max(y - pin.at[1], pin.at[1] - y - 3780, 0), 3780, pin.port)

    def test_nearest_spot(self):
        rows = [fakes.Row((0, 0, 10000, 3780), "R0"), fakes.Row((0, 3780, 10000, 7560), "MX"),
                fakes.Row((20000, 9000, 21000, 12780), "R0")]
        self.assertEqual(buffers.nearest_spot(rows, 5000, 8000, 2400), (3800, 3780, "MX"))
        self.assertEqual(buffers.nearest_spot(rows, 100, 100, 2400), (0, 0, "R0"))
        self.assertEqual(buffers.nearest_spot(rows, 9900, 100, 2400), (7600, 0, "R0"))
        # Just over a row's top edge, as ram_b's pins are: that row wins over a piece of row beside the macro,
        # whose bottom edge is nearer (LibreLane's diode rule measures only to bottom edges).
        beside = [rows[0], fakes.Row((12000, 4000, 22000, 7780), "MX")]
        self.assertEqual(buffers.nearest_spot(beside, 10000, 4100, 2400), (7600, 0, "R0"))
        # The third row is too narrow, however near.
        self.assertEqual(buffers.nearest_spot(rows, 20500, 9000, 2400)[1], 3780)


class BuffersFailLoudly(unittest.TestCase):
    def stops(self, block, master=BUF):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stopped:
            buffers.add_buffers(block, master, plugin.MACRO_PREFIX, plugin.PIN_NET_PREFIX)
        self.assertNotEqual(stopped.exception.code, 0)
        self.assertIn("[ERROR]", err.getvalue())
        return err.getvalue()

    def test_missing_macro(self):
        block = fakes.table_design()
        del block.insts["core.table_ram.ram_b"]
        self.assertIn("expected 2", self.stops(block))

    def test_missing_pin(self):
        block = fakes.table_design()
        macro = block.insts["core.table_ram.ram_b"]
        macro.terms.remove(macro.pin("A_BM[7]"))
        self.assertIn("75 live inputs", self.stops(block))

    def test_unconnected_pin(self):
        block = fakes.table_design()
        block.insts["core.table_ram.ram_b"].pin("A_DOUT[5]").disconnect()
        self.assertIn("not connected", self.stops(block))
        self.assertFalse(any(i.master is BUF for i in block.getInsts()), "changed the design before failing")

    def test_unplaced_pin(self):
        block = fakes.table_design()
        block.insts["core.table_ram.ram_a"].pin("A_WEN").at = None
        self.assertIn("no location", self.stops(block))

    def test_second_run(self):
        block, _ = buffered()
        self.assertIn("already has a buffer", self.stops(block))

    def test_not_a_buffer(self):
        self.assertIn("not a buffer", self.stops(fakes.table_design(), fakes.Master("tie", [("X", "output")])))

    def test_no_row(self):
        block = fakes.table_design()
        block.rows = []
        self.assertIn("no row", self.stops(block))


class Diodes(unittest.TestCase):
    def protect(self, block):
        inserter = diodes.MacroPinDiodes(types.SimpleNamespace(block=block), plugin.MACRO_PREFIX,
                                         plugin.PIN_NET_PREFIX)
        with contextlib.redirect_stdout(io.StringIO()):
            inserter.execute()
        return inserter

    def test_one_per_sram_input_where_the_long_wire_ends(self):
        block, _ = buffered()
        inserter = self.protect(block)
        self.assertEqual(inserter.count, 312)
        expected = []
        for macro in srams(block):
            for pin in macro.terms:
                if pin.direction != "input":
                    continue
                if pin.port in LIVE:
                    pin = next(t.inst for t in pin.net.terms if t is not pin).pin("A")
                expected.append((pin.net, pin))
        self.assertEqual(inserter.protected, expected)
        for net, term in inserter.protected:
            self.assertFalse(net.name.startswith(plugin.PIN_NET_PREFIX), "%s is a short net" % net.name)

    def test_follows_a_hold_buffer_put_in_front_of_the_pin_buffer(self):
        block, _ = buffered()
        pin = block.insts["sram_pin_buf_ram_a_A_WEN"].pin("A")
        hold = fakes.DbInst.create(block, BUF, "hold1")
        hold.pin("A").connect(pin.net)
        last = fakes.DbNet.create(block, "net1")
        hold.pin("X").connect(last)
        pin.connect(last)
        self.assertIn((last, pin), self.protect(block).protected)

    def test_without_buffers_every_diode_is_at_its_pin(self):
        block = fakes.table_design()
        inserter = self.protect(block)
        self.assertEqual(inserter.count, 312)
        self.assertTrue(all(term.inst.master.isBlock() for _, term in inserter.protected))

    def test_side_is_not_random(self):
        # With no source position, LibreLane's "source" strategy tosses a coin.
        made = {}

        class Recorder:
            count = 0

            def __init__(self, *args, **options):
                made.update(options)

            def execute(self):
                pass

        original, diodes.MacroPinDiodes = diodes.MacroPinDiodes, Recorder
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                diodes.sram_diodes(None, "cell", "A", plugin.MACRO_PREFIX, plugin.PIN_NET_PREFIX)
        finally:
            diodes.MacroPinDiodes = original
        self.assertEqual(made["side_strategy"], "pin")


class Flow(unittest.TestCase):
    def command(self, step_id, config):
        command = STEPS[step_id](config).get_command()
        return command[2], dict(zip(command[3::2], command[4::2]))

    def test_buffer_step_passes_what_its_script_takes(self):
        step = STEPS["Protoemu.SramPinBufferPlacement"]
        cell = {v.name: v.default for v in step.config_vars}
        script, args = self.command(step.id, cell)
        self.assertTrue(script.endswith("odb_sram_buffers.py") and os.path.isfile(script))
        self.assertEqual(set(args), set(BUFFER_OPTIONS))
        self.assertEqual(args["--buffer-cell"], "sg13cmos5l_buf_2")
        self.assertEqual(args["--net-prefix"], plugin.PIN_NET_PREFIX)
        self.assertTrue(fakes.SRAM.startswith(args["--macro-prefix"]))

    def test_diode_step_passes_what_its_script_takes(self):
        script, args = self.command("Protoemu.SramPinDiodePlacement", config())
        self.assertTrue(script.endswith("odb_sram_diodes.py") and os.path.isfile(script))
        self.assertEqual(set(args), {o for o in DIODE_OPTIONS if o.startswith("--")})
        self.assertEqual(args["--net-prefix"], plugin.PIN_NET_PREFIX)
        self.assertEqual((args["--diode-cell"], args["--diode-pin"]), ("sg13cmos5l_antennanp", "A"))

    def test_buffers_before_hold_repair_and_diodes_after(self):
        steps = config()["meta"]["substituting_steps"]
        self.assertEqual(steps["+OpenROAD.GeneratePDN"], "Protoemu.ExtendPowerStripes")
        self.assertEqual(steps["+OpenROAD.CTS"], "Protoemu.BuffersOnSramPins")
        self.assertEqual(steps["+OpenROAD.ResizerTimingPostCTS"], "Protoemu.DiodesOnSramPins")
        for step in steps.values():
            self.assertIn(step, STEPS)
        for step, script_step in (("Protoemu.BuffersOnSramPins", "Protoemu.SramPinBufferPlacement"),
                                  ("Protoemu.DiodesOnSramPins", "Protoemu.SramPinDiodePlacement")):
            self.assertEqual([s.id for s in STEPS[step].Steps], [script_step, "OpenROAD.DetailedPlacement"])

    def test_slew_and_cap_violations_fail_every_corner(self):
        self.assertEqual(config()["MAX_SLEW_VIOLATION_CORNERS"], ["*"])
        self.assertEqual(config()["MAX_CAP_VIOLATION_CORNERS"], ["*"])


if __name__ == "__main__":
    unittest.main()
