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
import types
import unittest
from pathlib import Path
from typing import Dict, List, Tuple
from unittest import mock

from .. import run
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


class Bug8Messages(Refusals):
    """Messages that named no line, another line or another reason. Every refusal names its line and shows it."""

    # (what follows HEAD, so its first line is line 4; the line; how the message starts; the source line shown)
    REFUSALS = (
        # declarations
        ("machine b\nmachine c\nmachine d\nmachine e\n", 7, "at most 4 machines", "machine e"),
        ("idle: halt\nmachine n\n", 5, "machine n has no states", "machine n"),
        ("in B\n", 4, "expected NAME=P0..P7, got `B`", "in B"),
        ("out T=P4\n", 4, "T is a reserved word", "out T=P4"),
        ("out X=P4\n", 4, "`out` pin X is declared twice", "out X=P4"),
        ("in B=P2, C=P3, D=P4\n", 4, "a machine has at most 3 `in` pins", "in B=P2, C=P3, D=P4"),
        ("out Y=P2, Z=P3, U=P4, V=P5\n", 4, "a machine has at most 4 `out` pins", "out Y=P2, Z=P3, U=P4, V=P5"),
        ("order up\n", 4, "order is lsb or msb", "order up"),
        ("order lsb\norder msb\n", 5, "order is set twice", "order msb"),
        ("crc init=1 init=2\n", 4, "crc init is set twice", "crc init=1 init=2"),
        ("crc init=-1\n", 4, "crc init = -1 is outside 0 to 0xFFFFFFFF", "crc init=-1"),
        ("crc poly=1 xinit=2\n", 4, "cannot evaluate `1 xinit=2`", "crc poly=1 xinit=2"),
        ("const X\n", 4, "expected `const NAME = expr`", "const X"),
        ("const R = 1\n", 4, "R is a reserved word", "const R = 1"),
        ("const Q = 1 2\n", 4, "cannot evaluate `1 2`", "const Q = 1 2"),
        ("const Q = 1e300 * 1e9\n", 4, "cannot evaluate `1e300 * 1e9`", "const Q = 1e300 * 1e9"),
        # labels, gotos and macros
        ("kz: halt\n", 4, "`kz` is a reserved word or a pin, not a label", "kz: halt"),
        ("idle: halt\nidle:\n\n      halt\n", 5, "label idle is defined twice", "idle:"),
        ("idle: halt\ntail:\n\n", 5, "label tail has no line after it", "tail:"),
        ("idle: halt\ntail:\n\nmachine n\n", 5, "label tail has no line after it", "tail:"),
        ("goto main\nmain: halt\n", 4, "a machine cannot start with `goto`", "goto main"),
        ("idle: X=1\n      goto out\n", 5, "machine m: goto out leads nowhere", "goto out"),
        ("idle: X=1\na:    goto b\nb:    goto a\n", 5, "machine m: goto b leads nowhere", "goto b"),
        ("macro p\n", 4, "expected `macro NAME(P, Q)`", "macro p"),
        ("macro p()\n", 4, "macro p has no `end`", "macro p()"),
        ("macro p()\nmacro q()\nend\n", 4, "macro p has no `end`", "macro p()"),
        ("macro p()\nend\nmacro p()\nend\n", 6, "macro p is defined twice", "macro p()"),
        ("macro p(V, V)\nend\n", 4, "macro p: each parameter is a name, and no two are the same", "macro p(V, V)"),
        ("idle: use q()\n", 4, "no macro named q", "idle: use q()"),
        ("macro p()\nend\nidle: use p(1)\n", 6, "p takes 0 arguments", "idle: use p(1)"),
        ("macro p()\nuse p()\nend\nidle: use p()\n", 7, "macros nest too deep", "use p()"),
        ("macro p()\nd: X=d => d\nend\nidle: use p()\n", 7, "`d` is a reserved word or a pin, not a label",
         "d: X=d => d"),
        # actions
        ("idle: X 1\n", 4, "cannot read `X 1`", "X 1"),
        ("idle: Y=1 when A\n", 4, "unknown register or pin `Y`", "Y=1 when A"),
        ("idle: A=1\n", 4, "`A` is an `in` pin; declare it with `out` as well to drive it", "A=1"),
        ("idle: S=1 when A\n", 4, "only pin operations can be gated with `when`", "S=1 when A"),
        ("idle: X=2\n", 4, "a pin is set to one of", "X=2"),
        ("idle: X=1, X=0, X=1\n", 4, "a state changes two pins at most", "X=1, X=0, X=1"),
        ("idle: X=1 when A & kz\n", 4, "pin operations are gated by one condition", "X=1 when A & kz"),
        ("idle: X=1 when A, X=0\n", 4, "pin operations in one state are all gated", "X=1 when A, X=0"),
        ("idle: X=d, X=c\n", 4, "`d` (shifter) and `c` (LFSR) cannot both be the data bit", "X=d, X=c"),
        ("idle: shift B\n", 4, "the serial input is 0, 1 or one of the first two `in` pins, not `B`", "shift B"),
        ("idle: shift, crc+=A\n", 4, "one state has one serial input", "shift, crc+=A"),
        ("idle: shift, shift\n", 4, "S shifts once per state", "shift, shift"),
        ("idle: shift, S=1\n", 4, "S is shifted or loaded in one state, not both", "shift, S=1"),
        ("idle: crc=0, crc>>=1\n", 4, "one LFSR operation per state", "crc=0, crc>>=1"),
        ("idle: crc=5\n", 4, "crc=0 clears the LFSR", "crc=5"),
        ("idle: K+=1\n", 4, "K and J only count down", "K+=1"),
        ("idle: K=16\n", 4, "K holds 0 to 15", "K=16"),
        ("idle: K=1, K-=1\n", 4, "K changes once per state", "K=1, K-=1"),
        ("idle: K=1, J=1\n", 4, "K and J cannot both be loaded in one state", "K=1, J=1"),
        ("idle: N-=1, N+=1\n", 4, "N changes once per state", "N-=1, N+=1"),
        ("idle: N=1, N-=1\n", 4, "N is loaded or counted in one state, not both", "N=1, N-=1"),
        ("idle: T=1, T=1\n", 4, "T is loaded once per state", "T=1, T=1"),
        ("idle: T=70000\n", 4, "T=70000 does not fit its field, 0 to 65535", "T=70000"),
        ("idle: T=-1\n", 4, "T=-1 does not fit its field, 0 to 65535", "T=-1"),
        ("idle: T=5, K=1\n", 4, "T=5 and K=1 set the same bits of the one immediate differently", "T=5, K=1"),
        ("idle: S=256\n", 4, "the bus carries one byte", "S=256"),
        ("idle: S=pop0, push1=S\n", 4, "one byte moves on the bus per state", "S=pop0, push1=S"),
        ("idle: S=2 ** 8\n", 4, "cannot evaluate `2 ** 8`: only numbers, constants", "S=2 ** 8"),
        ("idle: S=Q\n", 4, "cannot evaluate `Q`: no constant named Q", "S=Q"),
        ("idle: T=Q\n", 4, "cannot evaluate `Q`: no constant named Q", "T=Q"),
        ("idle: J=Q\n", 4, "cannot evaluate `Q`: no constant named Q", "J=Q"),
        # conditions and rules
        ("idle: wait Q\n", 4, "unknown condition `Q`", "wait Q"),
        ("idle: wait S==Q\n", 4, "cannot evaluate `Q`: no constant named Q", "wait S==Q"),
        ("idle: wait S==256\n", 4, "S==256 does not fit its field, 0 to 255", "wait S==256"),
        ("idle: X=1 =>\n", 4, "`=>` needs rules", "X=1 =>"),
        ("idle: X=1 => A: two words\n", 4, "bad rule `A: two words`", "X=1 => A: two words"),
        ("idle: => lost\n", 4, "no label `lost` in machine m", "=> lost"),
        ("idle: => A & kz & jz: idle\n", 4, "a state tests two conditions at most; this one needs 3",
         "=> A & kz & jz: idle"),
        ("idle: X=1\n", 4, "nothing follows this line", "X=1"),
        # wait, halt and hold
        ("idle: wait A, wait A\n", 4, "one `wait` or `halt` per line", "wait A, wait A"),
        ("idle: wait, K-=1\n", 4, "a waiting state repeats its actions every clock", "wait, K-=1"),
        ("idle: wait, hold 5\n", 4, "`hold` and `wait` do not go together", "wait, hold 5"),
        ("idle: halt, hold 5\n", 4, "`hold` and `halt` do not go together", "halt, hold 5"),
        ("idle: hold 5, hold 5\n", 4, "one `hold` per line", "hold 5, hold 5"),
        ("idle: hold Q\n", 4, "cannot evaluate `Q`: no constant named Q", "hold Q"),
        ("idle: hold 0\n", 4, "hold needs at least one clock", "hold 0"),
        ("idle: hold 70000\n", 4, "`hold` lasts 65537 turns at most", "hold 70000"),
        ("idle: K=1, hold 65538\n", 4, "`hold` lasts 65537 turns at most", "K=1, hold 65538"),
        ("idle: T=1, hold 5\n", 4, "`hold` uses the timer; this state already loads T", "T=1, hold 5"),
        ("idle: X=1 when S==3, hold 5\n", 4, "test `S==` on a line of its own, not on a `hold` line",
         "X=1 when S==3, hold 5"),
        ("idle: hold 5 => A: idle | kz: idle\n", 4,
         "the rules of a `hold` line test one condition besides the timer's `tz`; these test 2",
         "hold 5 => A: idle | kz: idle"),
        # call and ret
        ("idle: call idle, ret\n", 4, "`call` and `ret` cannot share a line", "call idle, ret"),
        ("idle: wait, ret\n", 4, "`call` and `ret` stand alone: no rules, `wait`, `halt` or `hold`", "wait, ret"),
        ("idle: ret, halt\n", 4, "`call` and `ret` stand alone: no rules, `wait`, `halt` or `hold`", "ret, halt"),
        ("idle: wait, call idle\n", 4, "`call` and `ret` stand alone", "wait, call idle"),
        ("idle: call idle => idle\n", 4, "`call` and `ret` stand alone", "call idle => idle"),
        ("idle: T=1, call idle\n", 4, "a call uses the immediate for its return address; move T=1", "T=1, call idle"),
        ("idle: N-=1, ret\n", 4, "`ret` and a change of N cannot share a line", "N-=1, ret"),
        ("idle: ret, N-=1\n", 4, "`ret` and a change of N cannot share a line", "ret, N-=1"),
        ("idle: call idle\n", 4, "nothing follows this call to return to", "call idle"),
    )

    def test_every_refusal_names_its_line_and_reason_and_shows_the_line(self) -> None:
        for body, line, reason, source in self.REFUSALS:
            with self.subTest(body=body):
                with self.assertRaises(CompileError) as caught:
                    build(body)
                error = caught.exception
                self.assertEqual((error.line, error.source), (line, source), str(error))
                self.assertTrue(error.message.startswith(reason), str(error))
                self.assertNotIn("<unknown>", error.message)  # Python's own idea of a file and a line

    def test_a_line_before_any_machine(self) -> None:
        with self.assertRaises(CompileError) as caught:
            compile_text("\nidle: halt\n", "f.fsm")
        self.assertEqual(str(caught.exception), "f.fsm:2: expected `machine NAME` first\n    idle: halt")

    def test_only_two_messages_are_about_the_whole_file(self) -> None:
        for text, reason in (("const A = 1\n", "no machine"),
                             (HEAD + "nop\n" * 513 + "halt\n", "the table is full: more than 512 entries")):
            with self.assertRaises(CompileError) as caught:
                compile_text(text, "f.fsm")
            self.assertEqual(str(caught.exception), "f.fsm: " + reason)

    def test_the_limits_themselves_compile(self) -> None:
        self.assertEqual(entries(build("idle:   hold 65537 => idle\n"), "m.idle")[0]["imm"], 65535)
        self.assertEqual(entries(build("idle:   K=1, hold 65537 => idle\n"), "m.idle.load")[0]["imm"], 65534)
        self.assertTrue(build("const BIG = 0x%s\nidle: halt\n" % ("F" * 256)).image)
        self.refused("const BIG = 0x1%s\n" % ("0" * 256), 4, "the number is too big")
        nested = "macro p0()\n  halt\nend\n" + "".join(
            "macro p%d()\n  use p%d()\nend\n" % (n, n - 1) for n in range(1, 8))
        self.assertTrue(build(nested + "idle: use p7()\n").image)
        with self.assertRaises(CompileError):
            build(nested + "macro p8()\n  use p7()\nend\nidle: use p8()\n")

    def test_a_short_hold_has_no_timer_to_test(self) -> None:
        for line in ("hold 2", "K=1, hold 3"):  # the rules sit on a `.pad` state, so two conditions fit
            self.assertTrue(build("idle:   %s => A: idle | kz: idle | idle\n" % line).image)

    def test_the_bakeoff_shows_file_line_and_reason(self) -> None:
        def program(name: str) -> bytes:
            return compile_text(HEAD + "idle: X=1, K+=1\n", "prog.fsm").image

        candidate = types.SimpleNamespace(program=program)
        with mock.patch.dict(sys.modules, {"bakeoff.cand_fake.candidate": candidate}):
            [row] = run.run_candidate("cand_fake", [types.SimpleNamespace(name="bench")])["rows"]
        self.assertEqual(row["detail"], "build: CompileError: prog.fsm:4: K and J only count down")


class Bug9ListingAsWritten(unittest.TestCase):
    """The listing printed `shift A` for `shift RX`, `crc+=in` for `crc+=RX` and `eq` for `S==7`."""

    def test_serial_inputs_print_by_pin_name(self) -> None:
        result = compile_text("machine m\n    in RX=P1, A=P2\n"
                              "first:  shift RX, crc+=RX\nsecond: shift A, crc+=A\n"
                              "third:  shift 1\nfourth: crc+=0 => first\n", "f.fsm")
        for state, wanted in (("first", "shift RX, crc+=RX "), ("second", "shift A, crc+=A "),
                              ("third", "shift 1 "), ("fourth", "crc+=0 ")):
            self.assertIn(wanted, listing_line(result, "m." + state))

    def test_a_compare_prints_its_byte(self) -> None:
        self.assertIn("| S==213 [", listing_line(build("idle:   wait S==0xD5\n        halt\n"), "m.idle"))
        gated = listing_line(build("idle:   X=1 when !S==7 => idle\n"), "m.idle")
        self.assertIn("(pins if !S==7)", gated)


class Bug10Files(unittest.TestCase):
    """Files that ended in a Python traceback, and what the command does with several files."""

    def run_compiler(self, *sources: bytes) -> Tuple[subprocess.CompletedProcess, List[str]]:
        with tempfile.TemporaryDirectory() as folder:
            paths = [str(Path(folder) / ("prog%d.fsm" % n)) for n in range(len(sources))]
            for path, source in zip(paths, sources):
                if source:  # an empty one stands for a file that is not there
                    Path(path).write_bytes(source)
            done = subprocess.run([sys.executable, "-m", "bakeoff.cand_table.compiler"] + paths, cwd=str(ROOT),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            return done, paths

    def test_a_byte_that_is_not_utf8(self) -> None:
        done, [path] = self.run_compiler(b"machine m\nidle: halt\n  T=\xe9\n")
        self.assertEqual(done.returncode, 1)
        self.assertTrue(done.stderr.startswith("%s:3: cannot evaluate" % path), done.stderr)
        done, _paths = self.run_compiler(b"machine m  # 5 \xb5s\nidle: halt\n")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_nothing_comes_before_the_message(self) -> None:
        done, [path] = self.run_compiler(b"machine m\nidle: T=1or 2\n")
        self.assertTrue(done.stderr.startswith("%s:2: cannot evaluate `1or 2`" % path), done.stderr)

    def test_a_byte_order_mark(self) -> None:
        done, _paths = self.run_compiler("\ufeffmachine m\nidle: halt\n".encode())
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_number_too_big_to_print(self) -> None:
        done, [path] = self.run_compiler(("machine m\ncrc init = %s * %s\n" % ("9" * 3000, "9" * 3000)).encode())
        self.assertEqual(done.returncode, 1)
        self.assertTrue(done.stderr.startswith("%s:2: cannot evaluate" % path), done.stderr)
        self.assertIn("the number is too big", done.stderr)

    def test_a_long_chain_of_gotos(self) -> None:
        chain = "".join("l%d: goto l%d\n" % (n, n + 1) for n in range(1500))
        done, _paths = self.run_compiler(("machine m\nidle: nop\n" + chain + "l1500: halt\n").encode())
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_every_file_is_tried_and_one_failure_is_status_1(self) -> None:
        good, bad = b"machine m\nidle: halt\n", b"machine m\nidle: K+=1\n"
        for sources in ((bad, good), (b"", good), (good, bad, b"", good)):
            done, paths = self.run_compiler(*sources)
            self.assertEqual(done.returncode, 1)
            self.assertNotIn("Traceback", done.stderr)
            for path, source in zip(paths, sources):
                wanted = {good: "%s: 1 states" % path, bad: "%s:2: K and J" % path,
                          b"": "%s: No such file or directory" % path}[source]
                self.assertIn(wanted, done.stdout if source == good else done.stderr)

    def test_a_state_never_reached_is_a_warning(self) -> None:
        done, _paths = self.run_compiler(b"machine m\nidle: halt\nlost: halt\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("warning: m.lost (line 3) is never reached", done.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
