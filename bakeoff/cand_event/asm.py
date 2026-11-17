"""Assembler for the event machine. One instruction per line; README.md lists the forms.

    python3 -m bakeoff.cand_event.asm bakeoff/cand_event/programs/uart_tx.s     # prints a listing

The image is `[words in lane 0: u16][words in lane 1: u16]` followed by the 32-bit words, little-endian.
"""
from __future__ import annotations

import ast
import operator
import re
import sys
from typing import Dict, List, Tuple

from . import isa


class AsmError(Exception):
    pass


OPERATORS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.LShift: operator.lshift, ast.RShift: operator.rshift,
    ast.BitOr: operator.or_, ast.BitAnd: operator.and_, ast.BitXor: operator.xor,
    ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Invert: operator.invert,
}


def _arithmetic(text: str, defs: Dict[str, int]) -> float:
    """A number written in a program: literals, `.def` names, brackets and + - * / // % << >> | & ^ ~.

    Nothing else is evaluated, so assembling a program never runs code that the program carries.
    """

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.Name) and node.id in defs:
            return defs[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            return OPERATORS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in OPERATORS:
            return OPERATORS[type(node.op)](walk(node.operand))
        if isinstance(node, ast.Name):
            raise ValueError("no .def named %s" % node.id)
        raise ValueError("only numbers, .def names, brackets and + - * / // % << >> | & ^ ~ are allowed")

    return walk(ast.parse(text.strip(), mode="eval").body)


def _expr(text: str, defs: Dict[str, int]) -> int:
    try:
        return int(round(_arithmetic(text, defs)))
    except Exception as exc:
        raise AsmError("bad expression %r: %s" % (text, exc))


def _event(text: str) -> int:
    text = text.strip()
    inv = 0
    if text.startswith("!"):
        inv, text = 1, text[1:].strip()
    if text not in isa.EVENTS:
        raise AsmError("unknown event %r" % text)
    return inv << 5 | isa.EVENTS[text]


def _reg(text: str) -> int:
    text = text.strip()
    if text not in ("r0", "r1", "r2", "r3"):
        raise AsmError("expected a register, got %r" % text)
    return int(text[1])


def _cfg(args: str, defs: Dict[str, int]) -> Tuple[int, int]:
    """`cfg <group> field=value flag ...` -> (destination, value)."""
    parts = args.split()
    group, items = parts[0], parts[1:]
    if group == "pins":     # cfg pins p0=tx p1=~tx p2=clk
        value = 0
        for item in items:
            pin, _, src = item.partition("=")
            value |= isa.PIN_SRC[src] << (2 * isa.PINSEL[pin])
        return isa.DESTS["pins.src"], value
    if group == "od":       # cfg od p0 p1
        return isa.DESTS["pins.od"], sum(1 << isa.PINSEL[item] for item in items)
    if group not in isa.FIELDS:
        raise AsmError("unknown cfg group %r" % group)
    dest, fields = isa.FIELDS[group]
    value = 0
    for item in items:
        name, eq, arg = item.partition("=")
        if name not in fields:
            raise AsmError("cfg %s has no field %r" % (group, name))
        shift, width, kind = fields[name]
        if not eq:
            number = 1 if width else 0
        elif kind == "minus1":
            number = _expr(arg, defs) - 1
        elif kind == "event":
            number = _event(arg)
        elif isinstance(kind, dict):
            if arg not in kind:
                raise AsmError("cfg %s %s cannot be %r" % (group, name, arg))
            number = kind[arg]
        else:
            number = _expr(arg, defs)
        if width and not 0 <= number < 1 << width:
            raise AsmError("cfg %s %s=%s does not fit %d bits" % (group, name, arg, width))
        value |= number << shift
    return isa.DESTS[dest], value


def _encode(text: str, labels: Dict[str, int], defs: Dict[str, int]) -> int:
    ev = 0
    if text.startswith("when "):
        head, colon, rest = text[5:].partition(":")
        ev = _event(head)
        text = rest.strip() if colon else ""
    def word(op: int, payload: int) -> int:
        return op << 28 | ev << 22 | payload

    def target(name: str) -> int:
        name = name.strip()
        if name not in labels:
            raise AsmError("unknown label %r" % name)
        return labels[name]

    if not text or text == "wait":
        return word(isa.ACT, 0)
    if "->" in text and not text.startswith("trap"):
        left, right = [s.strip() for s in text.split("->")]
        dests = [d.strip() for d in right.split(",")]
        if left.startswith("#"):
            if len(dests) != 1 or dests[0] not in isa.DESTS:
                raise AsmError("a constant goes to one destination: %r" % text)
            value = _expr(left[1:], defs)
            if not -0x8000 <= value <= 0xFFFF:
                raise AsmError("constant %d does not fit 16 bits" % value)
            return word(isa.LDI, isa.DESTS[dests[0]] << 16 | (value & 0xFFFF))
        payload = 0
        if left.endswith(".hi") and left[:-3] in isa.SOURCES:
            left, payload = left[:-3], 1 << 17
        if left not in isa.SOURCES:
            raise AsmError("unknown source %r" % left)
        payload |= isa.SOURCES[left] << 18
        if len(dests) > 2:
            raise AsmError("at most two destinations: %r" % text)
        if dests[0].endswith(".hi") and dests[0][:-3] in ("r0", "r1", "r2", "r3"):
            if len(dests) != 1:
                raise AsmError("a .hi destination stands alone: %r" % text)
            dests[0], payload = dests[0][:-3], payload | 1 << 16
        for shift, dest in zip((11, 6), dests):
            if dest not in isa.DESTS:
                raise AsmError("unknown destination %r" % dest)
            payload |= isa.DESTS[dest] << shift
        return word(isa.MOV, payload)
    op, _, args = text.partition(" ")
    args = args.strip()
    if op == "cfg":
        dest, value = _cfg(args, defs)
        return word(isa.LDI, dest << 16 | value)
    if op in isa.ALU_FN or op == "shl":
        parts = [a.strip() for a in args.split(",")]
        rd = _reg(parts[0])
        if op == "shl":         # a shift left is the register added to itself
            return word(isa.ALU, isa.ALU_FN["add"] << 19 | rd << 17 | 1 << 16 | rd)
        if op == "shr":
            return word(isa.ALU, isa.ALU_FN["shr"] << 19 | rd << 17)
        if len(parts) != 2:
            raise AsmError("%s takes a register and an operand" % op)
        if parts[1].startswith("#"):
            return word(isa.ALU, isa.ALU_FN[op] << 19 | rd << 17 | (_expr(parts[1][1:], defs) & 0xFFFF))
        return word(isa.ALU, isa.ALU_FN[op] << 19 | rd << 17 | 1 << 16 | _reg(parts[1]))
    if op == "act":
        payload = 0
        for name in args.split(","):
            name = name.strip()
            if name not in isa.STROBES:
                raise AsmError("unknown strobe %r" % name)
            payload |= 1 << isa.STROBES[name]
        return word(isa.ACT, payload)
    if op == "pin":
        payload = 0
        for item in args.split(","):
            pin, _, level = [s.strip() for s in item.partition("=")]
            if pin not in isa.PINSEL or level not in isa.PIN_OP:
                raise AsmError("pin takes p0..p7 = 0, 1 or z: %r" % item)
            payload |= isa.PIN_OP[level] << (2 * isa.PINSEL[pin])
        return word(isa.PIN, payload)
    if op == "br":
        cond, _, label = args.partition(",")
        ev = _event(cond)
        return word(isa.BR, target(label))
    if op == "goto":
        return word(isa.JMP, target(args))
    if op == "call":
        return word(isa.JMP, 1 << 9 | target(args))
    if op == "ret":
        return word(isa.RET, 0)
    if op == "resume":
        return word(isa.RET, 1)
    if op == "djnz":
        reg, _, label = args.partition(",")
        return word(isa.DJNZ, _reg(reg) << 9 | target(label))
    if op == "trap":
        if args == "off":
            return word(isa.TRAP, 0)
        cond, _, label = args.partition("->")
        return word(isa.TRAP, 1 << 15 | _event(cond) << 9 | target(label))
    if op == "halt":
        return word(isa.HALT, 0)
    raise AsmError("cannot parse %r" % text)


def assemble_lanes(source: str) -> List[List[int]]:
    defs: Dict[str, int] = {}
    labels: List[Dict[str, int]] = [dict() for _ in range(isa.N_LANES)]
    lines: List[List[Tuple[int, str]]] = [[] for _ in range(isa.N_LANES)]
    lane = 0
    for number, raw in enumerate(source.splitlines(), 1):
        text = raw.split(";")[0].strip().lower()
        try:
            if text.startswith(".lane"):
                lane = int(text.split()[1])
                continue
            if text.startswith(".def"):
                name, _, value = text[4:].partition("=")
                defs[name.strip()] = _expr(value, defs)
                continue
            match = re.match(r"^([a-z_][a-z0-9_]*):(.*)$", text)
            if match and not text.startswith("when "):
                if match.group(1) in labels[lane]:
                    raise AsmError("label %s is defined twice" % match.group(1))
                labels[lane][match.group(1)] = len(lines[lane])
                text = match.group(2).strip()
            if text:
                lines[lane].append((number, text))
        except (AsmError, ValueError, IndexError) as exc:
            raise AsmError("line %d: %s" % (number, exc))
    out: List[List[int]] = []
    for lane in range(isa.N_LANES):
        if len(lines[lane]) > isa.MEM_WORDS:
            raise AsmError("lane %d has %d words, the memory holds %d" % (lane, len(lines[lane]), isa.MEM_WORDS))
        words = []
        for number, text in lines[lane]:
            try:
                words.append(_encode(text, labels[lane], defs))
            except (AsmError, KeyError, ValueError) as exc:
                raise AsmError("line %d: %s" % (number, exc))
        out.append(words)
    return out


def assemble(source: str) -> bytes:
    lanes = assemble_lanes(source)
    image = bytearray()
    for words in lanes:
        image += len(words).to_bytes(2, "little")
    for words in lanes:
        for value in words:
            image += value.to_bytes(4, "little")
    return bytes(image)


if __name__ == "__main__":
    with open(sys.argv[1]) as handle:
        for lane_index, lane_words in enumerate(assemble_lanes(handle.read())):
            for address, value in enumerate(lane_words):
                print("lane %d  %03x  %08x" % (lane_index, address, value))
