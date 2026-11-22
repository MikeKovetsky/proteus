"""Tests of the compiler: programs that once compiled to something other than what was written.

    python3 -m bakeoff.cand_table.test_compiler

Each class is one bug, or one kind of bug, that the compiler had. A program that used to miscompile must now
compile to the entry fields it describes, decoded from the image with entry.py, or be refused with a message
that names the line and says why.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List

from . import entry as E
from .chip import TableChip
from .compiler import CompileError, Result, compile_text

ROOT = Path(__file__).resolve().parents[2]

# Line 1 is `machine m`, so a state's line number is its position below plus 3.
HEAD = "machine m\n    out X=P0\n    in  A=P1\n"


def build(body: str) -> Result:
    return compile_text(HEAD + body, "f.fsm")


def entries(result: Result, state: str) -> List[Dict[str, int]]:
    """The decoded fields of every entry that holds `state` (a state may be copied into several entries)."""
    _configs, words = E.parse_image(result.image)
    found = []
    for addr, line in enumerate(result.listing):
        if line.split()[1] == state:
            found.append(dict(zip(E.FIELDS, E.unpack(*words[addr]))))
    if not found:
        raise AssertionError("no entry holds %s:\n%s" % (state, "\n".join(result.listing)))
    return found


def address(result: Result, state: str) -> int:
    return next(addr for addr, line in enumerate(result.listing) if line.split()[1] == state)


def listing_line(result: Result, state: str) -> str:
    return next(line for line in result.listing if line.split()[1] == state)


class Refusals(unittest.TestCase):
    def refused(self, body: str, line: int, *wanted: str) -> str:
        with self.assertRaises(CompileError) as caught:
            build(body)
        message = str(caught.exception)
        self.assertTrue(message.startswith("f.fsm:%d: " % line), message)
        for text in wanted:
            self.assertIn(text, message)
        return message


class Bug1ReturnAndN(Refusals):
    """`N-=1, ret` dropped the N-=1. The N field holds 1 dec, 2 inc or 3 return: one entry cannot do both."""

    CALLER = "idle:   call sub\n        halt\n"  # lines 4 and 5; `sub` is line 6

    def test_dec_then_ret_is_refused(self) -> None:
        message = self.refused(self.CALLER + "sub:    N-=1, ret\n", 6, "cannot share a line", "N field",
                               "change N on the line before")
        self.assertIn("\n    N-=1, ret", message)  # the source line, under the message

    def test_ret_then_dec_is_refused_the_same_way(self) -> None:
        self.refused(self.CALLER + "sub:    ret, N-=1\n", 6, "cannot share a line", "change N on the line before")

    def test_inc_with_ret_is_refused_in_both_orders(self) -> None:
        self.refused(self.CALLER + "sub:    N+=1, ret\n", 6, "cannot share a line")
        self.refused(self.CALLER + "sub:    ret, N+=1\n", 6, "cannot share a line")

    def test_n_on_the_line_before_ret_compiles(self) -> None:
        result = build(self.CALLER + "sub:    X=1, N-=1\n        ret\n")
        [sub] = entries(result, "m.sub")
        [back] = entries(result, "m.line7")
        self.assertEqual(sub["nop"], E.N_DEC)
        self.assertEqual(sub["p0op"], E.P_HIGH)
        self.assertEqual(back["nop"], E.N_RETURN)

    def test_two_changes_of_n_are_still_refused(self) -> None:
        self.refused("idle:   N-=1, N+=1 => idle\n", 4, "N changes once per state")


class Bug2GatedPinsOnReturn(unittest.TestCase):
    """`X=1 when A, ret` compiled with cA on constant 0. A return ignores `base`, `cA` and `cB` for its next
    address, but cA still selects a flag every turn and gates the pins, so the entry can hold both."""

    def program(self, guard: str) -> Result:
        return build("idle:   call sub\n        halt\nsub:    X=1 when %s, ret\n" % guard)

    def test_gated_ret_selects_its_condition(self) -> None:
        [sub] = entries(self.program("A"), "m.sub")
        self.assertEqual(sub["ca"], E.C_A)
        self.assertEqual(sub["cb"], E.C_ZERO)
        self.assertEqual(sub["gate"], 1)  # pins if cA
        self.assertEqual((sub["p0"], sub["p0op"]), (0, E.P_HIGH))
        self.assertEqual(sub["nop"], E.N_RETURN)
        self.assertEqual(sub["base"], 0)

    def test_negated_gate_on_ret(self) -> None:
        [sub] = entries(self.program("!A"), "m.sub")
        self.assertEqual((sub["ca"], sub["gate"], sub["nop"]), (E.C_A, 2, E.N_RETURN))

    def test_gate_on_another_condition(self) -> None:
        [sub] = entries(self.program("kz"), "m.sub")
        self.assertEqual((sub["ca"], sub["gate"]), (E.C_KZ, 1))

    def run_model(self, result: Result, a: int) -> TableChip:
        chip = TableChip()
        chip.load(result.image)
        for _ in range(8):
            chip.step(a << 1)  # input A is P1
        return chip

    def test_model_drives_the_pin_only_when_the_gate_holds(self) -> None:
        for guard, a, driven in (("A", 1, True), ("A", 0, False), ("!A", 0, True), ("!A", 1, False)):
            result = self.program(guard)
            chip = self.run_model(result, a)
            self.assertEqual(bool(chip.oe & 1), driven, "when %s with A=%d" % (guard, a))
            if driven:
                self.assertEqual(chip.out & 1, 1)
            self.assertEqual(chip.mach[0].state, address(result, "m.line5"), "the return came back")

    def test_listing_names_the_gate(self) -> None:
        self.assertIn("(pins if A), ret", listing_line(self.program("A"), "m.sub"))


class Bug3CrcValues(Refusals):
    """`crc init = 1 + 2` set init to 1: the value stopped at the first space."""

    def config(self, crc_line: str) -> E.MachineConfig:
        result = compile_text("const START = 0xFF00\nmachine m\n    %s\nidle: halt\n" % crc_line, "f.fsm")
        [cfg], _words = E.parse_image(result.image)
        return cfg

    def test_a_value_with_spaces_evaluates_fully(self) -> None:
        self.assertEqual(self.config("crc init = 1 + 2").init, 3)

    def test_every_value_on_the_line(self) -> None:
        cfg = self.config("crc poly = 0x8C   init = START + 0xFF   match = 2 * 3")
        self.assertEqual((cfg.poly, cfg.init, cfg.match), (0x8C, 0xFFFF, 6))

    def test_values_without_spaces_are_unchanged(self) -> None:
        cfg = self.config("crc poly=0xEDB88320 init=0xFFFFFFFF match=0xDEBB20E3")
        self.assertEqual((cfg.poly, cfg.init, cfg.match), (0xEDB88320, 0xFFFFFFFF, 0xDEBB20E3))

    def test_trailing_words_are_refused(self) -> None:
        with self.assertRaises(CompileError) as caught:
            self.config("crc poly=1 junk")
        self.assertIn("f.fsm:3: cannot evaluate `1 junk`", str(caught.exception))

    def test_values_outside_32_bits_are_refused_not_masked(self) -> None:
        for text in ("crc init = 0x100000000", "crc init=-1"):
            with self.assertRaises(CompileError) as caught:
                self.config(text)
            self.assertIn("is outside 0 to 0xFFFFFFFF", str(caught.exception))


class Bug4ListingDataBit(unittest.TestCase):
    """The listing printed `d` for `c` (LFSR bit 0), and `c` for input C."""

    def test_pins_driven_from_the_lfsr_print_c(self) -> None:
        result = build("idle:   X=c, X=!c => idle\n")
        [idle] = entries(result, "m.idle")
        self.assertEqual((idle["p0op"], idle["p1op"], idle["dsel"]), (E.P_DATA, E.P_NDATA, 1))
        self.assertIn("X=c, X=!c", listing_line(result, "m.idle"))

    def test_pins_driven_from_the_shifter_still_print_d(self) -> None:
        result = build("idle:   X=d, X=!d => idle\n")
        self.assertIn("X=d, X=!d", listing_line(result, "m.idle"))

    def test_a_rule_on_the_lfsr_bit_prints_c(self) -> None:
        result = build("idle:   => c: idle | else: one\none:    X=1 => idle\n")
        for idle in entries(result, "m.idle"):  # idle is copied into the blocks that lead to it
            self.assertEqual((idle["ca"], idle["dsel"]), (E.C_D, 1))
        self.assertIn("| c [", listing_line(result, "m.idle"))

    def test_inputs_print_by_pin_name(self) -> None:
        result = compile_text("machine m\n    in A=P1, B=P2, CS=P3\nidle: wait => CS: idle | B: one\n"
                              "one: => A: idle | else: one\n", "f.fsm")
        self.assertIn("| CS,B [", listing_line(result, "m.idle"))
        self.assertIn("| A [", listing_line(result, "m.one"))


class Bug5CommandLine(unittest.TestCase):
    """A compile error on the command line printed a Python traceback."""

    def run_compiler(self, source: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prog.fsm"
            path.write_text(source)
            done = subprocess.run([sys.executable, "-m", "bakeoff.cand_table.compiler", str(path)], cwd=str(ROOT),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            done.path = str(path)  # type: ignore[attr-defined]
            return done

    def test_error_prints_file_line_message_and_exits_1(self) -> None:
        done = self.run_compiler("machine m\n    out X=P0\nidle: X=1, K+=1\n")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stderr, "%s:3: K and J only count down\n    X=1, K+=1\n" % done.path)
        self.assertEqual(done.stdout, "")

    def test_a_good_program_still_prints_its_listing(self) -> None:
        done = self.run_compiler("machine m\n    out X=P0\nidle: X=1 => idle\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("1 states in 1 entries", done.stdout)
        self.assertIn("m.idle", done.stdout)

    def test_errors_that_used_to_escape_as_python_exceptions(self) -> None:
        for body, line, wanted in (("idle: T=1e400 => idle\n", 4, "cannot evaluate `1e400`"),
                                   ("const X\nidle: X=1 => idle\n", 4, "expected `const NAME = expr`")):
            done = self.run_compiler(HEAD + body)
            self.assertEqual(done.returncode, 1, done.stderr)
            self.assertNotIn("Traceback", done.stderr)
            self.assertTrue(done.stderr.startswith("%s:%d: %s" % (done.path, line, wanted)), done.stderr)

    def test_compile_text_names_file_and_line(self) -> None:
        with self.assertRaises(CompileError) as caught:
            build("idle: X=1, K+=1\n")
        self.assertEqual(str(caught.exception), "f.fsm:4: K and J only count down\n    X=1, K+=1")


class Bug6SetTwice(Refusals):
    """`crc init=1 init=2` set init to 2. A setting or an action given twice is refused, not overwritten."""

    def test_crc_value_twice_on_one_line(self) -> None:
        self.refused("    crc init=1 init=2\nidle:   halt\n", 4, "crc init is set twice")
        self.refused("    crc poly=1 init=0 poly=1\nidle:   halt\n", 4, "crc poly is set twice")

    def test_crc_value_twice_on_two_lines(self) -> None:
        self.refused("    crc match=1\nidle:   halt\n    crc init=2 match=1\n", 6, "crc match is set twice")

    def test_crc_values_on_several_lines_and_per_machine(self) -> None:
        result = compile_text("machine a\n crc poly=1\n crc init=2\n order msb\nidle: halt\n"
                              "machine b\n crc init=3\n order lsb\nidle: halt\n", "f.fsm")
        a, b = E.parse_image(result.image)[0]
        self.assertEqual((a.poly, a.init, a.msb, b.poly, b.init, b.msb), (1, 2, 1, 0, 3, 0))

    def test_order_twice(self) -> None:
        self.refused("    order lsb\n    order msb\nidle:   halt\n", 5, "order is set twice")

    def test_a_pin_name_twice(self) -> None:
        self.refused("    out X=P4, Y=P2\nidle:   halt\n", 4, "`out` pin X is declared twice")
        self.refused("    in  B=P2, B=P3\nidle:   halt\n", 4, "`in` pin B is declared twice")

    def test_one_name_may_be_an_input_and_an_output(self) -> None:
        self.assertEqual(entries(build("    out A=P1\nidle:   A=z => A: idle | idle\n"), "m.idle")[0]["p0"], 1)

    def test_wait_or_halt_twice(self) -> None:
        for items in ("wait A, wait !A", "wait, wait A", "halt, wait A", "wait A, halt", "halt, halt"):
            self.refused("idle:   %s\n        halt\n" % items, 4, "one `wait` or `halt` per line")

    def test_hold_twice(self) -> None:
        self.refused("idle:   hold 5, hold 9 => idle\n", 4, "one `hold` per line")

    def test_shift_twice(self) -> None:
        for items in ("shift, shift", "shift A, shift A"):
            self.refused("idle:   %s => idle\n" % items, 4, "S shifts once per state")

    def test_n_loaded_and_counted(self) -> None:
        for items in ("N=5, N-=1", "N+=1, N=pop0"):
            self.refused("idle:   %s => idle\n" % items, 4, "N is loaded or counted in one state, not both")

    def test_n_writes_that_lose_nothing(self) -> None:
        [idle] = entries(build("idle:   Nhi=3, N-=1 => idle\n"), "m.idle")
        self.assertEqual((idle["dst"], idle["nop"]), (E.DST_NHI, E.N_DEC))
        [sub] = entries(build("idle:   call sub\n        halt\nsub:    N=5, ret\n"), "m.sub")
        self.assertEqual((sub["dst"], sub["nop"]), (E.DST_N, E.N_RETURN))

    def test_a_macro_twice(self) -> None:
        self.refused("macro p()\n  X=1\nend\nmacro p()\n  X=0\nend\nidle:   halt\n", 7, "macro p is defined twice")

    def test_a_parameter_twice_or_not_a_name(self) -> None:
        for params in ("V, V", "1", "V W"):
            self.refused("macro p(%s)\nend\nidle:   halt\n" % params, 4,
                         "each parameter is a name, and no two are the same")


class Bug7AsWritten(Refusals):
    """Programs that compiled to something other than their text says, and forms isa.md gives that did not compile."""

    def gate(self, body: str, state: str = "m.idle") -> Dict[str, int]:
        return entries(build(body), state)[0]

    def test_two_negations_cancel(self) -> None:
        self.assertEqual(self.gate("idle:   X=1 when !!A => idle\n")["gate"], 1)
        self.assertEqual(self.gate("idle:   X=1 when ! ! !A => idle\n")["gate"], 2)

    def test_a_negated_macro_argument(self) -> None:
        result = build("macro until(P)\n  wait !P\nend\nidle:   use until(!A)\ndone:   X=1\n        halt\n")
        for a in (0, 1):
            chip = TableChip()
            chip.load(result.image)
            for _ in range(8):
                chip.step(a << 1)
            self.assertEqual(chip.oe & 1, a, "`!P` given `!A` waits for A")

    def test_a_machine_cannot_start_with_goto(self) -> None:
        self.refused("        goto main\nsub:    halt\nmain:   halt\n", 4, "cannot start with `goto`", "nop => main")

    def test_a_constant_is_not_named_like_a_register(self) -> None:
        for name in ("R", "S", "Nlo", "T"):
            self.refused("const %s = 3\nidle:   halt\n" % name, 4, "%s is a reserved word" % name)

    def test_edge_needs_an_input(self) -> None:
        with self.assertRaises(CompileError) as caught:
            compile_text("machine m\nidle:   wait edge\n        halt\n", "f.fsm")
        self.assertEqual(caught.exception.line, 2)
        self.assertIn("none is declared", str(caught.exception))
        self.assertEqual(self.gate("idle:   X=1 when edge => idle\n")["ca"], E.C_EDGE)

    def test_a_number_is_no_name(self) -> None:
        self.refused("    in  1=P3\nidle:   halt\n", 4, "expected NAME=P0..P7, got `1=P3`")
        self.refused("const 1 = 5\nidle:   halt\n", 4, "expected `const NAME = expr`")

    def test_a_parameter_is_a_whole_word(self) -> None:
        result = build("macro load(xF, e3)\n  T=0xF + 1e3\n  K=xF, J-=1\n  halt\nend\nidle: use load(7, 9)\n")
        self.assertEqual(entries(result, "m.idle")[0]["imm"], 1015)

    def test_only_a_newline_ends_a_line(self) -> None:
        for break_like in ("\u2028", "\x0c", "\x85"):
            self.assertEqual(build("idle:   X=1 => idle  # note%sX=0\n" % break_like).states, 1)
        with self.assertRaises(CompileError) as caught:
            compile_text("machine m\r\n\r\nidle: K+=1\r\n", "f.fsm")
        self.assertEqual(caught.exception.line, 3)

    def test_commas_inside_brackets(self) -> None:
        self.assertEqual(self.gate("idle:   T=max(3, min(9, 7)), K-=1 => idle\n")["imm"], 7)
        self.assertEqual(self.gate("idle:   hold max(3, 9) => idle\n")["imm"], 7)
        self.assertEqual(self.gate("idle:   wait S==max(1, 2) => idle\n")["imm"], 2)
        self.assertEqual(self.gate("macro load(V)\n  T=V => here\nend\nhere:\nidle: use load(max(1, 4))\n",
                                   "m.here")["imm"], 4)

    def test_a_label_right_before_the_arrow(self) -> None:
        self.assertEqual(self.gate("idle:=> A: idle | idle\n")["base"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
