"""Assembler for cand_pio. `assemble(text)` returns the image the loader takes. See README.md for the syntax.

    python3 -m bakeoff.cand_pio.asm programs/uart_tx.pio     # print a listing
"""
from __future__ import annotations

import re
import sys
from typing import Dict, List, Optional, Tuple

from . import isa


class AsmError(Exception):
    pass


def _pin(tok: str) -> Tuple[int, int]:
    """'p3' -> (3, 1), 'v0' -> (8, 1), 'p0:2' -> (0, 2)."""
    m = re.fullmatch(r"([pv])([0-7])(?::(\d))?", tok)
    if not m:
        raise AsmError("not a pin: %r" % tok)
    return int(m.group(2)) + (8 if m.group(1) == "v" else 0), int(m.group(3) or 1)


def _is_pin(tok: str) -> bool:
    return re.fullmatch(r"[pv][0-7](?::\d)?", tok) is not None


class Assembler:
    def __init__(self) -> None:
        self.defines: Dict[str, int] = {}
        self.labels: Dict[str, Tuple[int, int]] = {}       # name -> (memory, address)
        self.items: List[List[tuple]] = [[], []]           # per memory: (line number, text, t, side, delay)
        self.cfg: Dict[int, int] = {}
        self.sm = 0

    def num(self, tok: str) -> int:
        tok = tok.strip()
        if tok in self.defines:
            return self.defines[tok]
        try:
            return int(tok, 0)
        except ValueError:
            raise AsmError("not a number: %r" % tok)

    def _ctrl(self, bits: int, value: int = 1, width: int = 1) -> None:
        reg = isa.CFG_CTRL0 + 2 * self.sm
        cur = self.cfg.get(reg, 0) & ~(((1 << width) - 1) << bits)
        self.cfg[reg] = cur | (value << bits)

    def directive(self, line: str) -> None:
        parts = line.split()
        name, args = parts[0], parts[1:]
        kv = dict(a.split("=", 1) for a in args if "=" in a)
        if name == ".sm":
            self.sm = self.num(args[0])
            self._ctrl(0)
            self._ctrl(1, len(self.items[self.sm >> 1]), 9)
        elif name == ".tick":
            period = float(args[0])
            raw = int(round(period * 256))
            self.cfg[isa.CFG_TIMER0 + 2 * self.sm] = raw & 0xFFFFFF
        elif name == ".shift":
            for key, bit in (("in", 10), ("out", 11)):
                if key in kv:
                    self._ctrl(bit, 1 if kv[key] == "left" else 0)
        elif name == ".autopush":
            self._ctrl(12)
            self._ctrl(14, 1 if "ch2" in args else 0)
        elif name == ".autopull":
            self._ctrl(13)
            self._ctrl(15, 1 if "ch3" in args else 0)
        elif name == ".side":
            base, count = _pin(args[0])
            self._ctrl(16, base, 4)
            self._ctrl(20, 1 if count == 2 else 0)
            self._ctrl(21, 1 if "dirs" in args else 0)
        elif name == ".pads":
            cur = self.cfg.get(isa.CFG_PADS, 0)
            for key, shift in (("out", 0), ("oe", 8), ("od", 16)):
                if key in kv:
                    cur = (cur & ~(0xFF << shift)) | (self.num(kv[key]) << shift)
            self.cfg[isa.CFG_PADS] = cur
        elif name == ".crc":
            self.cfg[isa.CFG_POLY] = self.num(kv["poly"])
            self.cfg[isa.CFG_RESIDUE] = self.num(kv.get("residue", "0"))
            tap = 8 | (4 if kv.get("tap", "in") == "out" else 0) | self.sm
            self.cfg[isa.CFG_PADS] = (self.cfg.get(isa.CFG_PADS, 0) & 0x00FFFFFF) | (tap << 24)
        elif name == ".define":
            self.defines[args[0]] = self.num(args[1])
        else:
            raise AsmError("unknown directive %s" % name)

    def first_pass(self, text: str) -> None:
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.split(";", 1)[0].strip()
            try:
                while True:
                    m = re.match(r"([A-Za-z_]\w*):\s*", line)
                    if not m:
                        break
                    mem = self.sm >> 1
                    if m.group(1) in self.labels:
                        raise AsmError("label %s defined twice" % m.group(1))
                    self.labels[m.group(1)] = (mem, len(self.items[mem]))
                    line = line[m.end():]
                if not line:
                    continue
                if line.startswith("."):
                    self.directive(line)
                    continue
                t = 0
                if "@tick" in line:
                    t, line = 1, line.replace("@tick", "")
                delay = 0
                m = re.search(r"\[([^\]]+)\]", line)
                if m:
                    delay, line = self.num(m.group(1)), line[:m.start()] + line[m.end():]
                side: Optional[int] = None
                m = re.search(r"\bside\s+(\S+)", line)
                if m:
                    side, line = self.num(m.group(1)), line[:m.start()] + line[m.end():]
                if not 0 <= delay <= 255:
                    raise AsmError("delay out of range")
                self.items[self.sm >> 1].append((lineno, line.strip(), t, side, delay))
            except AsmError as err:
                raise AsmError("line %d: %s (%s)" % (lineno, err, raw.strip()))

    def target(self, tok: str, mem: int, here: int) -> int:
        tok = tok.strip()
        if tok == ".":
            return here
        m = re.fullmatch(r"\.\s*([+-]\s*\d+)", tok)
        if m:
            return here + int(m.group(1).replace(" ", ""))
        if tok not in self.labels:
            raise AsmError("unknown label %r" % tok)
        lmem, addr = self.labels[tok]
        if lmem != mem:
            raise AsmError("label %r is in the other program memory" % tok)
        return addr

    def encode(self, mem: int, here: int, text: str, t: int, side: Optional[int], delay: int) -> int:
        parts = text.split(None, 1)
        mn = parts[0].lower()
        ops = [o.strip() for o in parts[1].split(",")] if len(parts) > 1 else []

        def w(op: int, arg: int, tt: int = t, dl: int = delay) -> int:
            return isa.word(op, arg, tt, side, dl)

        if mn == "nop":
            return w(isa.CTL, isa.K_NOP << 12)
        if mn == "jmp":
            cond = isa.C_ALWAYS if len(ops) == 1 else isa.CONDS[ops[0].replace(" ", "")]
            return w(isa.JMP, (cond << 12) | self.target(ops[-1], mem, here))
        if mn == "call":
            return w(isa.JMP, (isa.C_CALL << 12) | self.target(ops[0], mem, here))
        if mn == "ret":
            return w(isa.JMP, isa.C_RET << 12)
        if mn == "halt":
            return w(isa.JMP, here)
        if mn in ("jpin", "wait", "waitt"):
            level, (pin, _) = self.num(ops[0]), _pin(ops[1])
            arg = (level << 15) | (pin << 11)
            if mn == "jpin":
                return w(isa.JPIN, arg | self.target(ops[2], mem, here))
            if mn == "wait":
                return w(isa.WAIT, arg)
            if delay:
                raise AsmError("waitt takes its time-out as an operand, not a delay")
            arg |= self.target(ops[3], mem, here)
            if ops[2] == "tick":
                return w(isa.WAITT, arg, 1, 0)
            return w(isa.WAITT, arg, 0, self.num(ops[2]))
        if mn in ("in", "out", "outdir"):
            count = self.num(ops[1])
            if mn == "in":
                names = {"x": isa.S_X, "y": isa.S_Y, "null": isa.S_NULL, "ones": isa.S_ONES, "osr": isa.S_OSR}
                limit = 16
            else:
                names = {"x": isa.D_X, "y": isa.D_Y, "null": isa.D_NULL}
                limit = 32
            if not 1 <= count <= limit:
                raise AsmError("bit count out of range")
            if _is_pin(ops[0]):
                sel, base = (isa.D_DIRS if mn == "outdir" else 0), _pin(ops[0])[0]
                if count > 8:
                    raise AsmError("at most 8 pins at a time")
            else:
                sel, base = names[ops[0]], 0
            return w(isa.IN if mn == "in" else isa.OUT, (sel << 13) | (count << 7) | base)
        if mn == "push":
            ch, block, src, imm = 0, 1, isa.P_ISR, 0
            for o in ops:
                if o in ("ch1", "ch2"):
                    ch = 1 if o == "ch2" else 0
                elif o == "noblock":
                    block = 0
                elif o in ("isr", "x", "y"):
                    src = {"isr": isa.P_ISR, "x": isa.P_X, "y": isa.P_Y}[o]
                elif o.startswith("#"):
                    src, imm = isa.P_IMM, self.num(o[1:]) & 0xFF
                else:
                    raise AsmError("bad push operand %r" % o)
            return w(isa.PUSH, (ch << 15) | (block << 14) | (src << 12) | imm)
        if mn == "pull":
            ch = 1 if "ch3" in ops else 0
            return w(isa.PULL, (ch << 15) | ((0 if "noblock" in ops else 1) << 14))
        if mn == "mov":
            inv = 1 if ops[1].startswith("~") else 0
            src = isa.MOV_REGS[ops[1].lstrip("~").strip()]
            dst = isa.MOV_REGS[ops[0]]
            if dst > isa.R_OSR:
                raise AsmError("mov cannot write %s" % ops[0])
            count = self.num(ops[2]) if len(ops) > 2 else (32 if src in (isa.R_CRC, isa.R_OSR) else 16)
            return w(isa.MOV, (dst << 12) | (inv << 11) | (src << 7) | (count if dst == isa.R_OSR else 0))
        if mn in ("set", "dir", "toggle"):
            if mn == "set" and ops[0] in ("x", "y"):
                value = self.num(ops[1])
                if not 0 <= value <= 0x1FFF:
                    raise AsmError("set takes 0..8191")
                return w(isa.SET, ((isa.T_X if ops[0] == "x" else isa.T_Y) << 13) | value)
            base, count = _pin(ops[0])
            value = self.num(ops[1]) if len(ops) > 1 else (1 << count) - 1
            dst = {"set": isa.T_PINS, "dir": isa.T_DIRS, "toggle": isa.T_TOGGLE}[mn]
            return w(isa.SET, (dst << 13) | (base << 9) | ((count - 1) << 6) | (value & 63))
        if mn == "crcinit":
            return w(isa.CTL, (isa.K_CRCINIT << 12) | isa.CRC_INITS.index(self.num(ops[0])))
        if mn == "timer":
            return w(isa.CTL, (isa.K_TRESTART if ops[0] == "restart" else isa.K_THALF) << 12)
        raise AsmError("unknown instruction %r" % mn)

    def assemble(self, text: str) -> Tuple[bytes, List[List[int]]]:
        self.first_pass(text)
        words: List[List[int]] = [[], []]
        for mem in (0, 1):
            if len(self.items[mem]) > isa.MEM_WORDS:
                raise AsmError("program memory %d is full" % mem)
            for here, (lineno, body, t, side, delay) in enumerate(self.items[mem]):
                try:
                    words[mem].append(self.encode(mem, here, body, t, side, delay))
                except (AsmError, KeyError, IndexError, ValueError) as err:
                    raise AsmError("line %d: %s (%s)" % (lineno, err, body))
        out = bytearray([len(self.cfg)])
        out += len(words[0]).to_bytes(2, "little") + len(words[1]).to_bytes(2, "little")
        for reg in sorted(self.cfg):
            out += bytes([reg]) + self.cfg[reg].to_bytes(4, "little")
        for mem in (0, 1):
            for word in words[mem]:
                out += word.to_bytes(4, "little")
        return bytes(out), words


def assemble(text: str) -> bytes:
    return Assembler().assemble(text)[0]


def main() -> int:
    asm = Assembler()
    with open(sys.argv[1]) as handle:
        image, words = asm.assemble(handle.read())
    for mem in (0, 1):
        for addr, word in enumerate(words[mem]):
            print("%s%03x  %08x  %s" % ("AB"[mem], addr, word, asm.items[mem][addr][1]))
    print("%d configuration words, %d bytes" % (len(asm.cfg), len(image)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
