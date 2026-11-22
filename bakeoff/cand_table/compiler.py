"""Compiler from the state-machine language to a table image.

    python3 -m bakeoff.cand_table.compiler bakeoff/cand_table/programs/uart_tx.fsm

prints the listing: every table entry, the state it holds and where it goes.

The language, in short (docs/isa.md, section 4, has the full description):

    machine tx                      # one section per machine; machine m starts at its first line
        out TX=P0                   # up to four outputs and three inputs (A, B, C in the order given)
        order lsb                   # which end of the shifter is the data bit `d`
        const BIT = 521

    idle:   TX=1, wait rx0          # one line is one state; `wait` stays until a rule fires
            S=pop0, K=8             # actions take one clock, then the next line follows
            TX=0, hold BIT          # `hold n`: this line lasts n clocks in all
    bit:    TX=d, shift, K-=1, hold BIT => kz: stop | bit
    stop:   TX=1, hold BIT => idle

A line's rules (`=> guard: target | ...`) may test two conditions at most. The compiler turns each state's
rules into a block of two or four successors laid side by side, so the hardware picks the next state by
OR-ing the two condition bits into the block's address. A state that is the successor of several others
is copied into each block that needs it.

Passes: read lines -> lift macros out -> expand `use` -> parse each line into a State (`hold n` becomes
the states that spend n clocks) -> resolve labels and rules into successor tuples -> place the blocks ->
pack the entries.
"""
from __future__ import annotations

import ast
import operator
import re
import sys
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from . import entry as E


class CompileError(Exception):
    """A program the compiler refuses. It prints as `file:line: message`, then the source line, indented."""

    def __init__(self, message: str, line: Optional[int] = None, source: str = "", filename: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.source = source
        self.filename = filename

    def __str__(self) -> str:
        if self.filename:
            where = self.filename if self.line is None else "%s:%d" % (self.filename, self.line)
        else:
            where = "" if self.line is None else "line %d" % self.line
        text = "%s: %s" % (where, self.message) if where else self.message
        return text + ("\n    " + self.source if self.source else "")


# The N field of an entry holds either a change of N or the return code, never both.
RET_CHANGES_N = ("`ret` and a change of N cannot share a line: the entry's N field holds the return code, so "
                 "it has no room for N-=1 or N+=1; change N on the line before the `ret`")
NOT_A_LABEL = "`%s` is a reserved word or a pin, not a label"
HOLD_MAX = 0xFFFF + 2  # `hold n` counts n - 2 on the 16-bit timer
NUMBER_BITS = 1024     # no program needs a bigger number, and a message can always print one this size


BUILTIN_CONDS = {
    "edge": E.C_EDGE, "tz": E.C_TZ, "kz": E.C_KZ, "jz": E.C_JZ, "nz": E.C_NZ, "crc": E.C_CRC,
    "rx0": E.C_RX0, "rx3": E.C_RX3, "tx1": E.C_TX1, "tx2": E.C_TX2,
}
PIN_VALUES = {
    "0": (E.P_LOW, None), "1": (E.P_HIGH, None), "z": (E.P_RELEASE, None), "toggle": (E.P_TOGGLE, None),
    "d": (E.P_DATA, 0), "!d": (E.P_NDATA, 0), "od": (E.P_OD, 0), "c": (E.P_DATA, 1), "!c": (E.P_NDATA, 1),
}
BUS_DSTS = {"S": E.DST_S, "R": E.DST_R, "N": E.DST_N, "Nhi": E.DST_NHI, "push1": E.DST_PUSH1, "push2": E.DST_PUSH2}
BUS_SRCS = {"pop0": E.SRC_POP0, "pop3": E.SRC_POP3, "S": E.SRC_S, "R": E.SRC_R, "Nlo": E.SRC_NLO, "Nhi": E.SRC_NHI}
RESERVED = set(BUILTIN_CONDS) | set(BUS_DSTS) | set(BUS_SRCS) | {
    "T", "K", "J", "d", "c", "wait", "hold", "goto", "nop", "halt", "shift", "else", "when", "use", "call", "ret",
}
NEXT, STAY = "#next", "#stay"  # where a state goes when no rule fires; `#` starts a comment, so no source has these
LABEL_RE = re.compile(r"^([A-Za-z_]\w*)\s*:(?!=[^>])\s*(.*)$")  # `idle:=> kz: done` has a label too
IDENT_RE = re.compile(r"\b[A-Za-z_]\w*")  # whole words only: the xF in 0xF is no name
NAME = r"[^\W\d]\w*"  # a pin or a constant: not a number, which `shift 1` would read as a bit

Literal = Tuple[object, int]  # (condition key, wanted value); the key is a condition number or ("eq", byte)


class State:
    def __init__(self, line: int, text: str) -> None:
        self.line = line
        self.text = text
        self.name = ""
        self.fields = E.blank()
        self.imm_users: List[Tuple[int, int, str]] = []  # (mask, value, what) sharing the 16-bit immediate
        self.pins = 0
        self.dsel: Optional[int] = None
        self.din: Optional[int] = None
        self.gate: Optional[Literal] = None
        self.gated_seen: List[bool] = []
        self.rules: List[Tuple[List[Literal], str]] = []
        self.default = NEXT
        self.wait = False
        self.call: Optional[str] = None   # `call X`: go to X, and `ret` there comes back to the next line
        self.ret = False
        self.cont: Optional[int] = None   # the state a call returns to
        # Filled in by the back end.
        self.conds: List[object] = []
        self.targets: Tuple[int, ...] = ()

    def fail(self, message: str) -> None:
        raise CompileError(message, self.line, self.text)

    def use_imm(self, mask: int, value: int, what: str) -> None:
        if value & ~mask:
            self.fail("%s does not fit its field, 0 to %d" % (what, mask // (mask & -mask)))
        for other_mask, other_value, other_what in self.imm_users:
            overlap = mask & other_mask
            if (value & overlap) != (other_value & overlap):
                self.fail("%s and %s set the same bits of the one immediate differently; put them on separate lines"
                          % (other_what, what))
        self.imm_users.append((mask, value, what))

    def imm_clash(self, mask: int, value: int) -> bool:
        return any((value & mask & m) != (v & mask & m) for m, v, _ in self.imm_users)

    def use_dsel(self, which: int) -> None:
        if self.dsel is not None and self.dsel != which:
            self.fail("`d` (shifter) and `c` (LFSR) cannot both be the data bit of one state")
        self.dsel = which

    def use_din(self, which: int) -> None:
        if self.din is not None and self.din != which:
            self.fail("one state has one serial input")
        self.din = which


class Goto:
    def __init__(self, target: str, line: int, text: str) -> None:
        self.target = target
        self.line = line
        self.text = text


class Machine:
    def __init__(self, name: str, index: int, line: int, text: str) -> None:
        self.name = name
        self.index = index
        self.line = line
        self.text = text
        self.cfg = E.MachineConfig()
        self.outs: Dict[str, int] = {}
        self.ins: Dict[str, int] = {}
        self.items: List[object] = []           # State and Goto, in source order
        self.labels: Dict[str, int] = {}        # label -> position in items
        self.given: Set[str] = set()            # the settings the source gave: each applies to the whole machine
        self.states: List[State] = []


class Compiler:
    def __init__(self, text: str, filename: str = "<source>") -> None:
        self.filename = filename
        self.consts: Dict[str, float] = {}
        self.macros: Dict[str, Tuple[List[str], List[Tuple[int, str]]]] = {}
        self.machines: List[Machine] = []
        self.uses = 0
        self.lines = self._expand(self._macros(self._read(text)))

    # ---- front end: lines, constants, macros -----------------------------------------------------------

    def _read(self, text: str) -> List[Tuple[int, str]]:
        out = []
        # Not splitlines(): it also breaks at U+2028 and form feeds, where the rest of a comment would become code.
        for number, raw in enumerate(re.split(r"\r\n?|\n", text), 1):
            line = raw.split("#", 1)[0].strip()
            if line:
                out.append((number, line))
        return out

    def _macros(self, lines: List[Tuple[int, str]]) -> List[Tuple[int, str]]:
        """Take the macro definitions out of the source. A macro may be used before it is defined."""
        out: List[Tuple[int, str]] = []
        i = 0
        while i < len(lines):
            number, line = lines[i]
            head = re.match(r"^macro\s+(\w+)\s*\(([^)]*)\)$", line)
            if not head:
                if line.split()[0] == "macro":
                    raise CompileError("expected `macro NAME(P, Q)`", number, line)
                out.append(lines[i])
                i += 1
                continue
            body = []
            i += 1
            while i < len(lines) and lines[i][1] != "end" and lines[i][1].split()[0] != "macro":
                body.append(lines[i])
                i += 1
            if i == len(lines) or lines[i][1] != "end":
                raise CompileError("macro %s has no `end`" % head.group(1), number, line)
            params = [p.strip() for p in head.group(2).split(",") if p.strip()]
            if head.group(1) in self.macros:
                raise CompileError("macro %s is defined twice" % head.group(1), number, line)
            if len(set(params)) < len(params) or not all(IDENT_RE.fullmatch(p) for p in params):
                raise CompileError("macro %s: each parameter is a name, and no two are the same" % head.group(1),
                                   number, line)
            self.macros[head.group(1)] = (params, body)
            i += 1
        return out

    def _expand(self, lines: List[Tuple[int, str]], depth: int = 0) -> List[Tuple[int, str]]:
        """Replace every `use` by the macro's body, with the macro's own labels made unique."""
        out: List[Tuple[int, str]] = []
        i = 0
        while i < len(lines):
            number, line = lines[i]
            label = LABEL_RE.match(line)
            rest = label.group(2) if label else line
            use = re.match(r"^use\s+(\w+)\s*\((.*)\)$", rest)
            if use:
                if depth == 8:
                    raise CompileError("macros nest too deep", number, line)
                if use.group(1) not in self.macros:
                    raise CompileError("no macro named %s" % use.group(1), number, line)
                params, body = self.macros[use.group(1)]
                args = split_args(use.group(2))
                if len(args) != len(params):
                    raise CompileError("%s takes %d arguments" % (use.group(1), len(params)), number, line)
                self.uses += 1
                rename = dict(zip(params, args))
                for _, body_line in body:
                    local = LABEL_RE.match(body_line)
                    if local and local.group(1) not in rename:
                        if local.group(1) in RESERVED:  # renaming a `d` would rename the data bit all through the body
                            raise CompileError(NOT_A_LABEL % local.group(1), number, body_line)
                        rename[local.group(1)] = "%s_%d" % (local.group(1), self.uses)
                if label:
                    out.append((number, label.group(1) + ":"))
                expanded = [(number, IDENT_RE.sub(lambda m: rename.get(m.group(0), m.group(0)), text))
                            for _, text in body]
                out.extend(self._expand(expanded, depth + 1))
            else:
                out.append((number, line))
            i += 1
        return out

    def value(self, expr: str, line: int, source: str) -> int:
        try:
            result = int(round(arithmetic(expr, self.consts)))  # inside the try: 1e400 is infinity, no integer
            if result.bit_length() > NUMBER_BITS:
                raise ValueError("the number is too big")
            return result
        except SyntaxError as error:
            why = error.msg  # not str(error), which adds a line number of its own
        except Exception as error:
            why = str(error)
        raise CompileError("cannot evaluate `%s`: %s" % (expr.strip(), why), line, source)

    # ---- parsing ---------------------------------------------------------------------------------------

    def parse(self) -> None:
        machine: Optional[Machine] = None
        pending_labels: List[Tuple[str, int, str]] = []  # name, and the line that gave it
        for number, line in self.lines:
            words = line.split(None, 1)
            key, rest = words[0], (words[1] if len(words) > 1 else "")
            if key == "const":
                found = re.match(r"^(%s)\s*=(.*)$" % NAME, rest)
                if not found:
                    raise CompileError("expected `const NAME = expr`", number, line)
                if found.group(1) in RESERVED:  # `S=R` moves register R, whatever a constant R holds
                    raise CompileError("%s is a reserved word" % found.group(1), number, line)
                self.consts[found.group(1)] = self.value(found.group(2), number, line)
                continue
            if key == "machine":
                if pending_labels:
                    name, number, line = pending_labels[0]
                    raise CompileError("label %s has no line after it" % name, number, line)
                if len(self.machines) == E.MAX_MACHINES:
                    raise CompileError("at most %d machines" % E.MAX_MACHINES, number, line)
                machine = Machine(rest.strip() or "m%d" % len(self.machines), len(self.machines), number, line)
                self.machines.append(machine)
                continue
            if machine is None:
                raise CompileError("expected `machine NAME` first", number, line)
            if key in ("in", "out"):
                table, limit = (machine.ins, 3) if key == "in" else (machine.outs, 4)
                for part in rest.split(","):
                    found = re.match(r"^\s*(%s)\s*=\s*P([0-7])\s*$" % NAME, part)
                    if not found:
                        raise CompileError("expected NAME=P0..P7, got `%s`" % part.strip(), number, line)
                    if found.group(1) in RESERVED:
                        raise CompileError("%s is a reserved word" % found.group(1), number, line)
                    if found.group(1) in table:
                        raise CompileError("`%s` pin %s is declared twice" % (key, found.group(1)), number, line)
                    if len(table) == limit:
                        raise CompileError("a machine has at most %d `%s` pins" % (limit, key), number, line)
                    index = len(table)
                    table[found.group(1)] = index
                    (machine.cfg.inp if key == "in" else machine.cfg.out)[index] = int(found.group(2))
                continue
            if key == "order":
                if rest not in ("lsb", "msb"):
                    raise CompileError("order is lsb or msb", number, line)
                if key in machine.given:
                    raise CompileError("order is set twice: it holds for the whole machine", number, line)
                machine.given.add(key)
                machine.cfg.msb = 1 if rest == "msb" else 0
                continue
            if key == "crc" and re.match(r"^(poly|init|match)\s*=", rest):
                # Each value runs to the next `poly=`, `init=` or `match=`, so it may hold spaces: `init = 1 + 2`.
                parts = re.split(r"\b(poly|init|match)\s*=", rest)
                for name, expr in zip(parts[1::2], parts[2::2]):
                    if name in machine.given:
                        raise CompileError("crc %s is set twice: it holds for the whole machine" % name, number, line)
                    machine.given.add(name)
                    value = self.value(expr, number, line)
                    if not 0 <= value <= 0xFFFFFFFF:
                        raise CompileError("crc %s = %d is outside 0 to 0xFFFFFFFF, the LFSR's 32 bits"
                                           % (name, value), number, line)
                    setattr(machine.cfg, name, value)
                continue

            label = LABEL_RE.match(line)
            if label:
                if label.group(1) in RESERVED or label.group(1) in machine.ins or label.group(1) in machine.outs:
                    raise CompileError(NOT_A_LABEL % label.group(1), number, line)
                pending_labels.append((label.group(1), number, line))
                line = label.group(2)
                if not line:
                    continue
            position = len(machine.items)
            for name, at, source in pending_labels:
                if name in machine.labels:
                    raise CompileError("label %s is defined twice" % name, at, source)
                machine.labels[name] = position
            self._parse_step(machine, number, line, pending_labels[0][0] if pending_labels else "")
            pending_labels = []
        if pending_labels:
            name, number, line = pending_labels[0]
            raise CompileError("label %s has no line after it" % name, number, line)
        if not self.machines:
            raise CompileError("no machine")

    def _parse_step(self, machine: Machine, number: int, line: str, label: str) -> None:
        goto = re.match(r"^goto\s+(\w+)$", line)
        if goto:
            if not machine.items:
                raise CompileError("a machine cannot start with `goto`, which takes no turn; write `nop => %s`"
                                   % goto.group(1), number, line)
            machine.items.append(Goto(goto.group(1), number, line))
            return
        state = State(number, line)
        state.name = label
        actions, _, rules = line.partition("=>")
        hold = None
        halt = False
        until: Optional[List[Literal]] = None  # `wait COND`: the rules apply once COND holds
        for item in [" ".join(part.split()) for part in split_items(actions) if part.strip()]:
            if item == "nop":
                continue
            if item in ("wait", "halt") or item.startswith("wait "):
                if state.wait or halt:
                    state.fail("one `wait` or `halt` per line; join the conditions of a `wait` with `&`")
                halt = item == "halt"
                state.wait = not halt
                if item.startswith("wait "):
                    until = self._guard(machine, state, item[5:])
            elif item.startswith("hold "):
                if hold is not None:
                    state.fail("one `hold` per line")
                hold = self.value(item[5:], number, line)
            elif item == "ret":
                if state.fields["nop"] in (E.N_DEC, E.N_INC):
                    state.fail(RET_CHANGES_N)
                state.ret = True
                state.fields["nop"] = E.N_RETURN
            elif item.startswith("call "):
                state.call = item[5:].strip()
                self._bus(state, E.SRC_IMM, E.DST_LINK)
            else:
                self._action(machine, state, item)
        if rules.strip():
            for rule in rules.split("|"):
                guard, colon, target = rule.rpartition(":")
                target = target.strip()
                if not re.match(r"^\w+$", target):
                    state.fail("bad rule `%s`" % rule.strip())
                literals = [] if not colon or guard.strip() == "else" else self._guard(machine, state, guard)
                state.rules.append((literals, target))
        elif "=>" in line:
            state.fail("`=>` needs rules")
        if until is not None:
            state.rules = [(until + literals, target) for literals, target in state.rules]
            if not any(literals == until for literals, _ in state.rules):
                state.rules.append((until, NEXT))
        if state.call or state.ret:
            if state.call and state.ret:
                state.fail("`call` and `ret` cannot share a line")
            if state.rules or state.wait or halt or hold is not None:
                state.fail("`call` and `ret` stand alone: no rules, `wait`, `halt` or `hold` on their line")
            if state.call and state.imm_users:
                state.fail("a call uses the immediate for its return address; move %s to another line"
                           % state.imm_users[0][2])
            if state.call:
                state.rules.append(([], state.call))
        if state.wait or halt:
            state.default = STAY
            if hold is not None:
                state.fail("`hold` and `%s` do not go together" % ("halt" if halt else "wait"))
            f = state.fields
            if any(f[k] for k in ("src", "dst", "shift", "crc", "nop", "kj", "tload")) or E.P_TOGGLE in (f["p0op"], f["p1op"]):
                state.fail("a waiting state repeats its actions every clock, so it may only set pins")
        if state.fields["shift"] and state.fields["dst"] == E.DST_S:
            state.fail("S is shifted or loaded in one state, not both")
        if state.fields["dst"] == E.DST_N and state.fields["nop"] in (E.N_DEC, E.N_INC):
            state.fail("N is loaded or counted in one state, not both")  # a load would replace the count
        if any(state.gated_seen) and not all(state.gated_seen):
            state.fail("pin operations in one state are all gated by the same condition, or none is")
        self._lower(machine, state, hold)

    def _cond(self, machine: Machine, state: State, name: str) -> object:
        name = name.strip()
        if name in machine.ins:
            return (E.C_A, E.C_B, E.C_C)[machine.ins[name]]
        if name in BUILTIN_CONDS:
            if name == "edge" and not machine.ins:  # it would watch P0, where an undeclared input maps
                state.fail("`edge` watches the machine's first `in` pin, and none is declared")
            return BUILTIN_CONDS[name]
        if name in ("d", "c"):
            state.use_dsel(0 if name == "d" else 1)
            return E.C_D
        equal = re.match(r"^S\s*==\s*(.+)$", name)
        if equal:
            value = self.value(equal.group(1), state.line, state.text)
            state.use_imm(0x00FF, value, "S==%d" % value)
            return ("eq", value)
        state.fail("unknown condition `%s`" % name)

    def _guard(self, machine: Machine, state: State, text: str) -> List[Literal]:
        literals = []
        for part in text.split("&"):
            part = part.strip()
            want = 1 - part.count("!") % 2  # a macro's `!P` given `!A` pastes `!!A`
            literals.append((self._cond(machine, state, part.lstrip("! ")), want))
        return literals

    def _din(self, machine: Machine, state: State, name: str) -> int:
        if name in ("0", "1"):
            return int(name)
        if name in machine.ins and machine.ins[name] < 2:
            return E.DIN_A + machine.ins[name]
        state.fail("the serial input is 0, 1 or one of the first two `in` pins, not `%s`" % name)

    def _action(self, machine: Machine, state: State, item: str) -> None:
        f = state.fields
        if item in ("pop0", "pop3"):
            self._bus(state, BUS_SRCS[item], E.DST_NONE)
            return
        shift = re.match(r"^shift(?:\s+(\w+))?$", item)
        if shift:
            state.use_din(self._din(machine, state, shift.group(1) or "0"))
            if f["shift"]:
                state.fail("S shifts once per state")
            f["shift"] = 1
            return
        if re.match(r"^crc\s*>>=\s*1$", item):
            self._crc(state, E.CRC_SHIFT)
            return
        step = re.match(r"^crc\s*\+=\s*(\w+)$", item)
        if step:
            if step.group(1) == "d":
                state.use_dsel(0)
                self._crc(state, E.CRC_STEP_D)
            else:
                state.use_din(self._din(machine, state, step.group(1)))
                self._crc(state, E.CRC_STEP_IN)
            return
        count = re.match(r"^([KJN])\s*([-+])=\s*1$", item)
        if count:
            reg, sign = count.groups()
            if reg == "N":
                if state.ret:
                    state.fail(RET_CHANGES_N)
                if f["nop"]:
                    state.fail("N changes once per state")
                f["nop"] = E.N_DEC if sign == "-" else E.N_INC
            elif sign == "+":
                state.fail("K and J only count down")
            else:
                self._kj(state, reg, E.DEC)
            return
        assign = re.match(r"^(\w+)\s*=\s*(.+?)(?:\s+when\s+(.+))?$", item)
        if not assign:
            state.fail("cannot read `%s`" % item)
        lhs, rhs, when = assign.group(1), assign.group(2).strip(), assign.group(3)
        if lhs in machine.outs:
            if rhs not in PIN_VALUES:
                state.fail("a pin is set to one of: %s" % ", ".join(PIN_VALUES))
            op, dsel = PIN_VALUES[rhs]
            if dsel is not None:
                state.use_dsel(dsel)
            if state.pins == 2:
                state.fail("a state changes two pins at most")
            f["p%d" % state.pins], f["p%dop" % state.pins] = machine.outs[lhs], op
            state.pins += 1
            state.gated_seen.append(bool(when))
            if when:
                literal = self._guard(machine, state, when)
                if len(literal) != 1 or (state.gate is not None and state.gate != literal[0]):
                    state.fail("pin operations are gated by one condition")
                state.gate = literal[0]
            return
        if lhs in machine.ins:
            state.fail("`%s` is an `in` pin; declare it with `out` as well to drive it" % lhs)
        if lhs not in BUS_DSTS and lhs not in ("crc", "T", "K", "J"):
            state.fail("unknown register or pin `%s`" % lhs)
        if when:
            state.fail("only pin operations can be gated with `when`")
        if lhs == "crc":
            if rhs not in ("0", "init"):
                state.fail("crc=0 clears the LFSR, crc=init loads its configured start value")
            self._crc(state, E.CRC_ZERO if rhs == "0" else E.CRC_INIT)
        elif lhs == "T":
            if f["tload"]:
                state.fail("T is loaded once per state")
            f["tload"] = 1
            value = self.value(rhs, state.line, state.text)
            state.use_imm(0xFFFF, value, "T=%d" % value)
        elif lhs in ("K", "J"):
            value = self.value(rhs, state.line, state.text)
            if not 0 <= value <= 15:
                state.fail("%s holds 0 to 15" % lhs)
            self._kj(state, lhs, E.LOAD)
            state.use_imm(0x0F00 if lhs == "K" else 0xF000, value << (8 if lhs == "K" else 12), "%s=%d" % (lhs, value))
        else:  # a bus destination
            if rhs in BUS_SRCS:
                self._bus(state, BUS_SRCS[rhs], BUS_DSTS[lhs])
            else:
                value = self.value(rhs, state.line, state.text)
                if not 0 <= value <= 255:
                    state.fail("the bus carries one byte")
                self._bus(state, E.SRC_IMM, BUS_DSTS[lhs])
                state.use_imm(0x00FF, value, "%s=%d" % (lhs, value))

    def _bus(self, state: State, src: int, dst: int) -> None:
        f = state.fields
        if f["dst"] or f["src"]:
            state.fail("one byte moves on the bus per state")
        f["src"], f["dst"] = src, dst

    def _crc(self, state: State, op: int) -> None:
        if state.fields["crc"]:
            state.fail("one LFSR operation per state")
        state.fields["crc"] = op

    def _kj(self, state: State, reg: str, op: int) -> None:
        k_op, j_op = divmod(state.fields["kj"], 3)
        if (k_op if reg == "K" else j_op):
            state.fail("%s changes once per state" % reg)
        k_op, j_op = (op, j_op) if reg == "K" else (k_op, op)
        if k_op == E.LOAD and j_op == E.LOAD:
            state.fail("K and J cannot both be loaded in one state")
        state.fields["kj"] = 3 * k_op + j_op

    def _lower(self, machine: Machine, state: State, hold: Optional[int]) -> None:
        """Turn `hold n` into the states that spend n clocks."""
        if hold is None or hold == 1:
            machine.items.append(state)
            return
        if hold < 1:
            state.fail("hold needs at least one clock")
        if hold > HOLD_MAX:
            state.fail("`hold` lasts %d turns at most, all that the 16-bit timer counts" % HOLD_MAX)
        rules, state.rules = state.rules, []
        if any(what.startswith("S==") for _, _, what in state.imm_users):
            state.fail("test `S==` on a line of its own, not on a `hold` line")
        tests_data_bit = any(lit[0] == E.C_D for lits, _ in rules for lit in lits)
        machine.items.append(state)

        def extra(suffix: str) -> State:
            more = State(state.line, state.text)
            more.name = (state.name or "line%d" % state.line) + suffix
            machine.items.append(more)
            return more

        timer = hold - 2
        if hold >= 3 and (state.fields["tload"] or state.imm_clash(0xFFFF, timer)):
            if state.fields["tload"]:
                state.fail("`hold` uses the timer; this state already loads T")
            if hold == 3:
                extra(".pad")
                hold = 2
            else:
                loader = extra(".load")
                loader.fields["tload"] = 1
                loader.use_imm(0xFFFF, hold - 3, "T")
        elif hold >= 3:
            state.fields["tload"] = 1
            state.use_imm(0xFFFF, timer, "T")
        if hold == 2:
            last = extra(".pad")
            last.rules = rules
            last.dsel = state.dsel if tests_data_bit else None
            return
        tested = {key for literals, _ in rules for key, _ in literals} - {E.C_TZ}
        if len(tested) > 1:  # only here: the `.pad` of a short hold has no timer to test and takes two
            state.fail("the rules of a `hold` line test one condition besides the timer's `tz`; these test %d"
                       % len(tested))
        waiter = extra(".hold")
        waiter.wait = True
        waiter.default = STAY
        timeout: Literal = (E.C_TZ, 1)
        has_else = False
        for literals, target in rules:
            waiter.rules.append(([timeout] + literals, target))
            has_else = has_else or not literals
        if not has_else:
            waiter.rules.append(([timeout], NEXT))
        waiter.dsel = state.dsel if tests_data_bit else None

    # ---- back end: targets, placement, encoding --------------------------------------------------------

    def _resolve(self, machine: Machine) -> None:
        items = machine.items
        position_of_state: Dict[int, int] = {}
        for position, item in enumerate(items):
            if isinstance(item, State):
                position_of_state[position] = len(machine.states)
                machine.states.append(item)

        def at(position: int) -> Optional[int]:
            """The state reached by entering `items` at `position`, following gotos."""
            seen: Set[int] = set()
            while position < len(items) and isinstance(items[position], Goto):
                goto = items[position]
                if position in seen or goto.target not in machine.labels:
                    raise CompileError("machine %s: goto %s leads nowhere" % (machine.name, goto.target),
                                       goto.line, goto.text)
                seen.add(position)
                position = machine.labels[goto.target]
            return position_of_state.get(position)

        for position, item in enumerate(items):
            if not isinstance(item, State):
                continue
            here = position_of_state[position]
            if not item.name:
                item.name = "line%d" % item.line
            following = at(position + 1)

            def target(name: str) -> int:
                if name == STAY:
                    return here
                if name == NEXT:
                    if following is None:
                        item.fail("nothing follows this line")
                    return following
                if name not in machine.labels:
                    item.fail("no label `%s` in machine %s" % (name, machine.name))
                return at(machine.labels[name])  # always a state: parse refuses a label with no line after it

            if item.ret:
                # The link register decides where a return goes, so it has no successors in the table. Its cA
                # still selects a flag every turn, and a `when` on the line gates the pins with that flag.
                item.conds = [item.gate[0]] if item.gate is not None else []
                continue
            if item.call:
                if following is None:
                    item.fail("nothing follows this call to return to")
                item.cont = following
            conds: List[object] = []
            if item.gate is not None:
                conds.append(item.gate[0])
            for literals, _ in item.rules:
                for key, _want in literals:
                    if key not in conds:
                        conds.append(key)
            if len(conds) > 2:
                item.fail("a state tests two conditions at most; this one needs %d" % len(conds))
            targets = []
            for index in range(1 << len(conds)):
                env = {key: (index >> n) & 1 for n, key in enumerate(conds)}
                chosen = item.default
                for literals, name in item.rules:
                    if all(env[key] == want for key, want in literals):
                        chosen = name
                        break
                targets.append(target(chosen))
            # Drop a condition that changes nothing. The gate's condition must stay: it is cA.
            if len(conds) == 2 and targets[0] == targets[2] and targets[1] == targets[3]:
                conds, targets = conds[:1], targets[:2]
            elif len(conds) == 2 and item.gate is None and targets[0] == targets[1] and targets[2] == targets[3]:
                conds, targets = conds[1:], [targets[0], targets[2]]
            if len(conds) == 1 and item.gate is None and targets[0] == targets[1]:
                conds, targets = [], targets[:1]
            item.conds = conds
            item.targets = tuple(targets)

    def compile(self) -> "Result":
        self.parse()
        for machine in self.machines:
            if not any(isinstance(item, State) for item in machine.items):
                raise CompileError("machine %s has no states" % machine.name, machine.line, machine.text)
            self._resolve(machine)

        Key = Tuple[int, int]  # (machine, state)
        blocks: List[Tuple[Key, ...]] = []
        seen_blocks = set()
        reached = set()
        work: List[Key] = []
        for machine in self.machines:
            first = (machine.index, 0)
            work.append(first)
            reached.add(first)
        while work:
            mi, si = work.pop()
            state = self.machines[mi].states[si]
            wanted = [tuple((mi, t) for t in state.targets)]
            if state.cont is not None:
                wanted.append(((mi, state.cont),))  # a return address needs an entry of its own to point at
            for block in wanted:
                if block and block not in seen_blocks:
                    seen_blocks.add(block)
                    blocks.append(block)
                for key in block:
                    if key not in reached:
                        reached.add(key)
                        work.append(key)

        placed: Dict[int, Key] = {m.index: (m.index, 0) for m in self.machines}
        base_of: Dict[Tuple[Key, ...], int] = {}
        for block in sorted(blocks, key=lambda b: -len(b)):  # big blocks first, so small ones can reuse them
            size = len(block)
            home = None
            for addr in range(0, E.DEPTH, size):  # a block that already holds these states, in this order
                if all(placed.get(addr + i) == key for i, key in enumerate(block)):
                    home = addr
                    break
            if home is None:
                for addr in range(0, E.DEPTH, size):  # or the first block whose slots are free or agree
                    if all(placed.get(addr + i, key) == key for i, key in enumerate(block)):
                        home = addr
                        break
            if home is None:
                raise CompileError("the table is full: more than %d entries" % E.DEPTH)
            for i, key in enumerate(block):
                placed[home + i] = key
            base_of[block] = home

        count = max(placed) + 1
        words: List[Tuple[int, int]] = []
        listing: List[str] = []
        for addr in range(count):
            if addr not in placed:
                words.append((0, 0))
                listing.append("%3d  (unused)" % addr)
                continue
            mi, si = placed[addr]
            machine = self.machines[mi]
            state = machine.states[si]
            f = dict(state.fields)
            f["base"] = base_of[tuple((mi, t) for t in state.targets)] if state.targets else 0
            cond_numbers = [E.C_EQ if isinstance(key, tuple) else key for key in state.conds] + [0, 0]
            f["ca"], f["cb"] = cond_numbers[0], cond_numbers[1]
            f["dsel"] = state.dsel or 0
            f["din"] = state.din or 0
            f["gate"] = 0 if state.gate is None else (1 if state.gate[1] else 2)
            imm = 0
            for _mask, value, _what in state.imm_users:
                imm |= value
            if state.cont is not None:
                imm = base_of[((mi, state.cont),)]
            f["imm"] = imm
            words.append(E.pack(f))
            listing.append("%3d  %-22s %s" % (addr, "%s.%s" % (machine.name, state.name), describe(f, machine, state)))
        image = E.build_image([m.cfg for m in self.machines], words)
        unreached = ["%s.%s (line %d)" % (m.name, state.name, state.line)
                     for m in self.machines for si, state in enumerate(m.states) if (m.index, si) not in reached]
        return Result(image, listing, count, len(reached), count - len(placed), unreached)


def describe(f: Dict[str, int], machine: Machine, state: State) -> str:
    """One listing line for an entry: what it does and where it goes."""
    outs = {index: name for name, index in machine.outs.items()}
    ins = {E.C_A + index: name for name, index in machine.ins.items()}
    data = "c" if f["dsel"] else "d"  # the data bit: the shifter's end bit, or LFSR bit 0
    serial = str(f["din"]) if f["din"] < E.DIN_A else ins[E.C_A + f["din"] - E.DIN_A]

    def cond(c: int) -> str:
        if c == E.C_D:
            return data
        if c == E.C_EQ:
            return "S==%d" % (f["imm"] & 0xFF)
        return ins.get(c, E.CONDS[c])

    parts = []
    for slot in ("p0", "p1"):
        op = f[slot + "op"]
        if op:
            value = {E.P_DATA: data, E.P_NDATA: "!" + data}.get(op, E.PIN_OPS[op])
            parts.append("%s=%s" % (outs.get(f[slot], "O%d" % f[slot]), value))
    if f["gate"]:
        parts.append("(pins if %s%s)" % ("" if f["gate"] == 1 else "!", cond(f["ca"])))
    if f["dst"] == E.DST_LINK:
        parts.append("call, back to %d" % f["imm"])
    elif f["dst"] or f["src"] in (E.SRC_POP0, E.SRC_POP3):
        source = "%d" % (f["imm"] & 0xFF) if f["src"] == E.SRC_IMM else E.SRCS[f["src"]]
        parts.append("%s=%s" % (E.DSTS[f["dst"]], source) if f["dst"] else source)
    if f["shift"]:
        parts.append("shift %s" % serial)
    if f["crc"] == E.CRC_STEP_IN:
        parts.append("crc+=%s" % serial)
    elif f["crc"]:
        parts.append("crc%s" % E.CRC_OPS[f["crc"]])
    if f["nop"]:
        parts.append(("N-=1", "N+=1", "ret")[f["nop"] - 1])
    k_op, j_op = divmod(f["kj"], 3)
    for reg, op, shift in (("K", k_op, 8), ("J", j_op, 12)):
        if op:
            parts.append("%s-=1" % reg if op == E.DEC else "%s=%d" % (reg, (f["imm"] >> shift) & 15))
    if f["tload"]:
        parts.append("T=%d" % f["imm"])
    conds = [cond(c) for c in (f["ca"], f["cb"]) if c]
    names = [machine.states[t].name for t in state.targets]
    if state.ret:
        return "%-40s -> link" % (", ".join(parts) or "-")
    where = "-> %d" % f["base"] if not conds else "-> %d | %s" % (f["base"], ",".join(conds))
    return "%-40s %s [%s]" % (", ".join(parts) or "-", where, " ".join(names))


ARITHMETIC_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos,
}
ARITHMETIC_CALLS = {"round": round, "int": int, "min": min, "max": max}


def arithmetic(expr: str, names: Dict[str, float]) -> float:
    """Evaluate a number written in a program: literals, constants, + - * / // %, brackets, round, int, min, max.

    Nothing else is evaluated, so compiling a program never runs code that the program carries.
    """

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.Name) and node.id in names:
            return names[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in ARITHMETIC_OPS:
            return ARITHMETIC_OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in ARITHMETIC_OPS:
            return ARITHMETIC_OPS[type(node.op)](walk(node.operand))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ARITHMETIC_CALLS
                and not node.keywords):
            return ARITHMETIC_CALLS[node.func.id](*[walk(arg) for arg in node.args])
        if isinstance(node, ast.Name):
            raise ValueError("no constant named %s" % node.id)
        raise ValueError("only numbers, constants, + - * / // %, round, int, min and max are allowed")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # newer Pythons warn on stderr about `1or`, ahead of the error
        tree = ast.parse(expr.strip(), mode="eval")
    return walk(tree.body)


def split_items(text: str) -> List[str]:
    """`text` split at the commas outside brackets and braces: `T=max(BIT, 4), K=8` is two items."""
    items, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch in "({":
            depth += 1
        elif ch in ")}":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(text[start:i])
            start = i + 1
    items.append(text[start:])
    return items


def split_args(text: str) -> List[str]:
    """Macro arguments, split at commas. An argument in braces may hold commas: {shift, crc+=d}."""
    args = [re.sub("[{}]", "", item).strip() for item in split_items(text)]
    return [] if args == [""] else args


class Result:
    def __init__(self, image: bytes, listing: List[str], entries: int, states: int, unused: int,
                 unreached: List[str]) -> None:
        self.image = image
        self.listing = listing
        self.entries = entries      # table entries the image fills
        self.states = states        # states the source describes and the machines can reach
        self.unused = unused        # holes left by block alignment
        self.unreached = unreached  # states no machine can reach: left out of the table


def compile_text(text: str, filename: str = "<source>") -> Result:
    try:
        return Compiler(text, filename).compile()
    except CompileError as error:
        error.filename = error.filename or filename
        raise


def compile_file(path: Path) -> Result:
    # A byte-order mark is dropped. A byte that is not UTF-8 is harmless in a comment; elsewhere its line is refused.
    return compile_text(Path(path).read_text(encoding="utf-8-sig", errors="replace"), str(path))


def main() -> int:
    """Print each file's listing. A file that does not compile prints `file:line: message` and the exit
    status is 1."""
    status = 0
    for name in sys.argv[1:]:
        try:
            result = compile_file(Path(name))
        except CompileError as error:
            print(error, file=sys.stderr)
            status = 1
            continue
        except OSError as error:
            print("%s: %s" % (name, error.strerror or error), file=sys.stderr)
            status = 1
            continue
        print("%s: %d states in %d entries (%d unused), image %d bytes"
              % (name, result.states, result.entries, result.unused, len(result.image)))
        print("\n".join(result.listing))
        for unreached in result.unreached:
            print("warning: %s is never reached" % unreached)
    return status


if __name__ == "__main__":
    sys.exit(main())
