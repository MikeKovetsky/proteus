"""Cycle-accurate model of cand_pio: four PIO-style state machines on two synchronous program memories.

One `step` is one 60 MHz clock. Machines 0 and 1 fetch from memory A, machines 2 and 3 from memory B. A memory
has one read port: its address is latched on one clock and the word is on its output during the next. A machine
that is alone on its memory executes every clock (its next address goes straight to the address pins); two
machines on one memory take alternate clocks, so each runs at 30 MHz.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..harness.chip import Chip
from . import isa
from .isa import (CTL, IN, JMP, JPIN, MOV, OUT, PULL, PUSH, SET, WAIT, WAITT)

FIFO_DEPTH = 4
M16, M32 = 0xFFFF, 0xFFFFFFFF


class _SM:
    """The flops of one state machine."""

    def __init__(self) -> None:
        self.en = 0
        self.pc = self.lr = 0                    # 9 + 9
        self.x = self.y = 0                      # 16 + 16
        self.isr = self.isr_n = 0                # 16 + 5
        self.osr = self.osr_n = 0                # 32 + 6
        self.cnt = 0                             # 8: delay and time-out counter
        self.dly = self.gate = self.wt = False   # 3: in a delay / tick gate passed / WAITT running
        self.t_int = self.t_frac = 0             # 16.8 timer period (configuration)
        self.t_cnt = self.t_acc = 0              # 16 + 8
        self.tick = False                        # 1: a tick is pending for this machine's next clock
        self.in_left = self.out_left = self.autopush = self.autopull = 0
        self.push_ch, self.pull_ch = 1, 0
        self.side_base, self.side_n, self.side_dirs = 0, 1, 0


class PioChip(Chip):
    name = "pio"

    def __init__(self) -> None:
        self.reset()

    # Reset and load.

    def reset(self) -> None:
        self.sm = [_SM() for _ in range(isa.N_SM)]
        self.mem: List[List[tuple]] = [[isa.decode(0)] * isa.MEM_WORDS for _ in range(2)]
        self.rd_addr = [0, 0]          # the address each memory latched on the last clock
        self.cycle = 0
        self.pout = 0                  # pin values: bits 0-7 are the pad output registers, 8-15 internal pins
        self.poe = 0                   # pad output enables
        self.od = 0                    # open-drain pads: a 1 releases the pad, a 0 pulls it low
        self.crc = self.poly = self.residue = 0
        self.crc_tap = 0               # bit 3 enable, bit 2 0=IN 1=OUT, bits 1:0 machine
        self.crcok = False             # registered compare of crc against residue
        self.fifo: List[List[int]] = [[] for _ in range(4)]
        self.active: List[int] = []

    def load(self, image: bytes) -> None:
        """What the loader does: write configuration registers, then both program memories."""
        n_cfg = image[0]
        n_a = image[1] | (image[2] << 8)
        n_b = image[3] | (image[4] << 8)
        pos = 5
        for _ in range(n_cfg):
            self._write_cfg(image[pos], int.from_bytes(image[pos + 1:pos + 5], "little"))
            pos += 5
        for mem, count in ((0, n_a), (1, n_b)):
            for i in range(count):
                self.mem[mem][i] = isa.decode(int.from_bytes(image[pos:pos + 4], "little"))
                pos += 4
        self.active = [k for k in range(isa.N_SM) if self.sm[k].en]
        for m in (0, 1):
            self.rd_addr[m] = self.sm[self._owner(m, 0)].pc

    def _write_cfg(self, reg: int, v: int) -> None:
        if reg == isa.CFG_PADS:
            self.pout, self.poe, self.od, self.crc_tap = v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 15
        elif reg == isa.CFG_POLY:
            self.poly = v
        elif reg == isa.CFG_RESIDUE:
            self.residue = v
        elif reg >= isa.CFG_TIMER0:
            s = self.sm[(reg - isa.CFG_TIMER0) >> 1]
            if (reg - isa.CFG_TIMER0) & 1 == 0:
                s.t_frac, s.t_int = v & 0xFF, (v >> 8) & M16
                s.t_cnt = max(s.t_int - 1, 0)
            else:
                s.en, s.pc = v & 1, (v >> 1) & 0x1FF
                s.in_left, s.out_left = (v >> 10) & 1, (v >> 11) & 1
                s.autopush, s.autopull = (v >> 12) & 1, (v >> 13) & 1
                s.push_ch, s.pull_ch = (2 if (v >> 14) & 1 else 1), (3 if (v >> 15) & 1 else 0)
                s.side_base, s.side_n, s.side_dirs = (v >> 16) & 15, 1 + ((v >> 20) & 1), (v >> 21) & 1

    def _owner(self, mem: int, cycle: int) -> int:
        """Which machine uses memory `mem` on `cycle`."""
        a, b = 2 * mem, 2 * mem + 1
        if self.sm[a].en and self.sm[b].en:
            return a + (cycle & 1)
        return b if self.sm[b].en else a

    # Host link.

    def host_can_push(self, ch: int) -> bool:
        return ch in (0, 3) and len(self.fifo[ch]) < FIFO_DEPTH

    def host_push(self, ch: int, byte: int) -> None:
        self.fifo[ch].append(byte & 0xFF)

    def host_pop(self, ch: int) -> Optional[int]:
        if ch in (1, 2) and self.fifo[ch]:
            return self.fifo[ch].pop(0)
        return None

    # One clock.

    def step(self, pins: int) -> Tuple[int, int]:
        cycle = self.cycle
        pins_now = (pins & 0xFF) | (self.pout & 0xFF00)   # pads through the synchroniser, internal pins direct
        crc_before = self.crc
        sms = self.sm
        for k in self.active:
            s = sms[k]
            if s.t_int:                                   # the timer runs on every clock
                if s.t_cnt == 0:
                    s.tick = True
                    acc = s.t_acc + s.t_frac
                    s.t_acc = acc & 0xFF
                    s.t_cnt = s.t_int - 1 + (acc >> 8)
                else:
                    s.t_cnt -= 1
        for m in (0, 1):
            k = self._owner(m, cycle)
            s = sms[k]
            if s.en:
                ins = self.mem[m][self.rd_addr[m]]        # the word the memory latched an address for last clock
                assert self.rd_addr[m] == s.pc
                self._slot(k, s, ins, pins_now)
                s.tick = False
            self.rd_addr[m] = sms[self._owner(m, cycle + 1)].pc
        self.crcok = crc_before == self.residue           # one clock behind the CRC register
        self.cycle = cycle + 1
        out = self.pout & 0xFF
        return out & ~self.od, self.poe & ~(self.od & out)

    def _slot(self, k: int, s: _SM, ins: tuple, pins: int) -> None:
        op, t, se, sv, dl, a, b, c, d = ins
        if s.dly:                                         # delay after the previous instruction
            s.cnt -= 1
            if s.cnt == 0:
                s.dly = False
            return
        if t and op != WAITT and not s.gate:              # T: start on the tick
            if not s.tick:
                return
            s.gate = True
        if se:                                            # side-set applies from the instruction's first clock
            if s.side_dirs:
                self._write_dirs(s.side_base, s.side_n, sv)
            else:
                self._write_pins(s.side_base, s.side_n, sv)
        nxt = (s.pc + 1) & 0x1FF

        if op == JMP:
            if a == isa.C_ALWAYS:
                take = True
            elif a == isa.C_NX:
                take = s.x == 0
            elif a == isa.C_XDEC:
                take = s.x != 0
                s.x = (s.x - 1) & M16
            elif a == isa.C_NY:
                take = s.y == 0
            elif a == isa.C_YDEC:
                take = s.y != 0
                s.y = (s.y - 1) & M16
            elif a == isa.C_XNEY:
                take = s.x != s.y
            elif a == isa.C_NOSRE:
                take = s.osr_n != 0
            elif a == isa.C_NISRE:
                take = s.isr_n != 0
            elif a == isa.C_CRCOK:
                take = self.crcok
            elif a == isa.C_NCRCOK:
                take = not self.crcok
            elif a == isa.C_CALL:
                take = True
                s.lr = nxt
            else:                                         # C_RET
                take = True
                b = s.lr
            if take:
                nxt = b
        elif op == JPIN:
            if (pins >> b) & 1 == a:
                nxt = c
        elif op == WAIT:
            if (pins >> b) & 1 != a:
                return
        elif op == WAITT:
            if not s.wt:
                s.wt, s.cnt = True, dl
            if (pins >> b) & 1 != a:
                if not (s.tick if t else s.cnt == 0):
                    if not t:
                        s.cnt -= 1
                    return
                nxt = c                                   # timed out
            s.wt, s.cnt = False, 0
            dl = 0
        elif op == IN:
            if s.autopush and s.isr_n + b >= 8 and len(self.fifo[s.push_ch]) >= FIFO_DEPTH:
                return                                    # no room for the byte this shift completes
            if a == isa.S_PINS:
                v = (pins | (pins << 16)) >> c
            elif a == isa.S_X:
                v = s.x
            elif a == isa.S_Y:
                v = s.y
            elif a == isa.S_ONES:
                v = M32
            elif a == isa.S_OSR:
                v = s.osr
            else:
                v = 0
            v &= (1 << b) - 1
            if s.in_left:
                s.isr = ((s.isr << b) | v) & M16
            else:
                s.isr = ((s.isr >> b) | (v << (16 - b))) & M16
            s.isr_n = min(s.isr_n + b, 16)
            if b == 1 and self.crc_tap == (8 | k):
                self._crc_step(v)
            if s.autopush and s.isr_n >= 8:
                self.fifo[s.push_ch].append(s.isr & 0xFF if s.in_left else s.isr >> 8)
                s.isr = s.isr_n = 0
        elif op == OUT:
            if s.autopull and s.osr_n == 0:               # empty: refill this clock, shift on the next
                self._pull(s, s.pull_ch)
                return
            if s.out_left:
                v = s.osr >> (32 - b)
                s.osr = (s.osr << b) & M32
            else:
                v = s.osr & ((1 << b) - 1)
                s.osr >>= b
            s.osr_n = max(s.osr_n - b, 0)
            if a == isa.D_PINS:
                self._write_pins(c, b, v)
            elif a == isa.D_X:
                s.x = v & M16
            elif a == isa.D_Y:
                s.y = v & M16
            elif a == isa.D_DIRS:
                self._write_dirs(c, b, v)
            if b == 1 and self.crc_tap == (12 | k):
                self._crc_step(v)
            if s.autopull and s.osr_n == 0:               # refill at once if a byte is waiting
                self._pull(s, s.pull_ch)
        elif op == PUSH:
            if len(self.fifo[a]) >= FIFO_DEPTH:
                if b:
                    return
            else:
                if c == isa.P_ISR:
                    byte = s.isr & 0xFF if s.in_left else s.isr >> 8
                else:
                    byte = (s.x, s.y, d)[c - 1] & 0xFF
                self.fifo[a].append(byte)
            if c == isa.P_ISR:
                s.isr = s.isr_n = 0
        elif op == PULL:
            if not self._pull(s, a):
                if b:
                    return
                byte = s.x & 0xFF                         # as in the RP2040: an empty non-blocking pull copies X
                s.osr, s.osr_n = (byte << 24 if s.out_left else byte), 8
        elif op == MOV:
            if c == isa.R_X:
                v, n = s.x, 16
            elif c == isa.R_Y:
                v, n = s.y, 16
            elif c == isa.R_ISR:
                v, n = s.isr, 16
            elif c == isa.R_OSR:
                v, n = s.osr, 32
            elif c == isa.R_PINS:
                v, n = pins, 16
            elif c == isa.R_CRC:
                v, n = self.crc, 32
            else:
                v, n = 0, 32
            if b:
                v = ~v & M32
            if a == isa.R_X:
                s.x = v & M16
            elif a == isa.R_Y:
                s.y = v & M16
            elif a == isa.R_ISR:
                s.isr, s.isr_n = v & M16, 0
            elif a == isa.R_OSR:
                s.osr, s.osr_n = v & M32, d
        elif op == SET:
            if a == isa.T_X:
                s.x = b
            elif a == isa.T_Y:
                s.y = b
            elif a == isa.T_PINS:
                self._write_pins(c, d, b)
            elif a == isa.T_DIRS:
                self._write_dirs(c, d, b)
            else:                                         # toggle
                for i in range(d):
                    if (b >> i) & 1:
                        self.pout ^= 1 << ((c + i) & 15)
        elif op == CTL:
            if a == isa.K_CRCINIT:
                self.crc = isa.CRC_INITS[b & 3]
            elif a == isa.K_TRESTART:
                s.t_cnt, s.t_acc = max(s.t_int - 1, 0), 0
            elif a == isa.K_THALF:
                s.t_cnt, s.t_acc = max((s.t_int >> 1) - 1, 0), 0

        s.pc = nxt
        s.gate = False
        if dl:
            s.cnt, s.dly = dl, True

    def _pull(self, s: _SM, ch: int) -> bool:
        if not self.fifo[ch]:
            return False
        byte = self.fifo[ch].pop(0)
        s.osr, s.osr_n = (byte << 24 if s.out_left else byte), 8
        return True

    def _write_pins(self, base: int, count: int, value: int) -> None:
        for i in range(count):
            bit = 1 << ((base + i) & 15)
            self.pout = (self.pout | bit) if (value >> i) & 1 else (self.pout & ~bit)

    def _write_dirs(self, base: int, count: int, value: int) -> None:
        for i in range(count):
            bit = 1 << ((base + i) & 15)
            if bit < 256:
                self.poe = (self.poe | bit) if (value >> i) & 1 else (self.poe & ~bit)

    def _crc_step(self, bit: int) -> None:
        self.crc = (self.crc >> 1) ^ (self.poly if (self.crc ^ bit) & 1 else 0)

    def budget(self) -> Dict[str, object]:
        per_sm = {
            "pc and link register": 18, "x and y": 32, "isr and its bit count": 21, "osr and its bit count": 38,
            "delay / time-out counter": 8, "run state": 3, "timer period (16.8)": 24, "timer counter": 24,
            "tick pending": 1, "shift, auto-push/pull and side-set settings": 13,
        }
        shared = {
            "pad out and enable registers": 16, "open-drain mask": 8, "internal pins": 8,
            "pad input synchronisers (modelled by the harness)": 16,
            "crc register, polynomial, residue, tap, compare flag": 101,
            "four FIFOs of %d bytes with pointers" % FIFO_DEPTH: 4 * (FIFO_DEPTH * 8 + 7),
            "memory slot phase": 1, "loader and host link (estimate)": 100,
        }
        sm_bits, shared_bits = sum(per_sm.values()), sum(shared.values())
        return {
            "flops": isa.N_SM * sm_bits + shared_bits,
            "sram_bits": 2 * isa.MEM_WORDS * 32,
            "gates_estimate": 7000,
            "engines": 4,
            "notes": "4 machines at 30 MHz, or 2 at 60 MHz (one per program memory). %d flops per machine, "
                     "%d shared. Gates: about 1,500 per machine and 900 shared, not synthesised."
                     % (sm_bits, shared_bits),
            "per_machine": per_sm,
            "shared": shared,
        }
