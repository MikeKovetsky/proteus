"""Cycle-accurate model of the event machine: two lanes, each an event-driven engine with its own helpers.

Everything here is written as synchronous logic. In one `step` the engine decides from the state at the start
of the cycle, the helpers advance from that same state, and the engine's writes land at the end of the cycle.
README.md describes the machine; isa.py holds the encodings.
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

from ..harness.chip import Chip
from . import isa

M16 = 0xFFFF
M32 = 0xFFFFFFFF

# Flip-flops, counted per block. `budget()` and the README both come from this table.
LANE_FLOPS = {
    "engine: pc 9, link 9, trap link 9, trap 16, flags 2, state 3": 48,
    "registers r0-r3": 64,
    "timers t0,t1: count 16, period 16, state 4, retrigger 6 each": 84,
    "sampler: count 16, period 16, phase 16, config 12, state 5": 65,
    "receive shifter: live 16, capture 16, count 5, flags 2, config 8": 47,
    "transmit shifter and line coder: shift 16, holding 16, count 5, state 6, config 13": 56,
    "run-length unit: limit 4, count 4, error 1": 9,
    "crc unit: register 32, polynomial 32, byte 8, count 4, config 2": 78,
    "pattern matcher: value 16, mask 16, source 1, flag 1": 34,
    "strobe register": 13,
    "pin routing: source 16, open-drain 8": 24,
}
SHARED_FLOPS = {
    "pad registers: out 8, oe 8, input synchronisers 16": 32,
    "host FIFOs: 4 x (4 bytes + pointers 5)": 148,
    "host link and program loader": 70,
    "cross-lane flags": 2,
}
SRAM_BITS = isa.N_LANES * isa.MEM_WORDS * 32
# Combinational cells, estimated per block from the mux, adder and comparator widths.
LANE_GATES = {
    "instruction decode, two 32:1 event muxes, ready logic": 250,
    "move bus: 12:1 x 16 source mux, byte lanes, write decode": 300,
    "ALU: 16-bit add/subtract, logic, operand muxes, flags": 310,
    "program counter and next-address mux": 60,
    "timers: increment, compare, retrigger mux, twice": 320,
    "sampler: decrement, reload muxes, edge logic": 130,
    "receive shifter: insert mux, counter, line decode": 150,
    "transmit shifter: load mux, bit taps, counter, coder": 160,
    "run-length unit": 25,
    "crc unit: 32 x (xor + mux), byte shifter": 120,
    "pattern matcher": 60,
    "configuration write enables": 80,
    "pin routing": 50,
}
SHARED_GATES = {
    "host FIFOs: read muxes, write decode, pointers": 290,
    "pin resolution and open drain": 60,
    "host link": 250,
    "program loader": 150,
}


class Lane:
    """One engine and its private helpers."""

    def __init__(self, chip: "EventChip", index: int) -> None:
        self.chip = chip
        self.index = index
        self.reset()

    def reset(self) -> None:
        self.mem: List[int] = [0] * isa.MEM_WORDS
        self.run = False        # the engine is executing
        self.live = False       # a program was loaded, so the helpers are clocked
        # Engine.
        self.pc = 0
        self.fetch = True       # this cycle is the SRAM read
        self.extra = 0          # second cycle of DJNZ
        self.op = self.sel = self.inv = self.pay = 0
        self.lr = self.tlr = 0
        self.trap_en = False
        self.trap_sel = self.trap_inv = self.trap_tgt = 0
        self.z = self.c = 0
        self.r = [0, 0, 0, 0]
        self.pend: List[Tuple[int, int]] = []
        self.strobes = 0
        # Timers.
        self.t_cnt = [0, 0]
        self.t_lim = [2, 2]
        self.t_run = [False, False]
        self.t_flag = [0, 0]
        self.t_auto = [0, 0]
        self.t_rsel = [0, 0]
        self.t_rinv = [0, 0]
        self.t_tick0 = 0        # one-cycle pulse: timer 0 expired, the transmitter's bit clock
        # Sampler.
        self.smp_cnt = 0
        self.smp_period = self.smp_phase = 2
        self.smp_clk = self.smp_data = self.smp_edgemode = self.smp_mode = self.smp_inv = self.smp_nrzi = 0
        self.smp_armed = self.smp_running = False
        self.smp_last = 1
        self.smp_tick = self.smp_edge = 0
        # Receive shifter.
        self.rx_live = self.rx_cap = self.rx_cnt = self.rx_full = self.rx_prev = 0
        self.rx_nbits = 1
        self.rx_msb = self.rx_clock = self.rx_destuff = 0
        # Transmit shifter and coder.
        self.tx_sh = self.tx_hold = self.tx_hfull = self.tx_cnt = 0
        self.tx_busy = self.tx_half = self.tx_line = self.tx_bit = 0
        self.tx_nbits = 1
        self.tx_msb = self.tx_code = self.tx_inv = self.tx_split = self.tx_stuff = 0
        self.tx_init = self.tx_clkinv = self.tx_ext = 0
        # Run-length unit.
        self.rl_n = self.rl_cnt = self.rl_err = 0
        # CRC.
        self.crc = self.crc_poly = self.crc_data = self.crc_n = 0
        self.crc_reflect = 0
        self.crc_xor = 0
        # Matcher.
        self.m_val = self.m_mask = self.m_src = self.match = 0
        # Pin routing.
        self.m_tx = self.m_ntx = self.m_clk = self.m_any = 0
        self.od = 0

    # ---- events -------------------------------------------------------------------------------------

    def event(self, sel: int, pins: int) -> int:
        if sel <= 8:
            return (pins >> (sel - 1)) & 1 if sel else 1
        if sel == 9:
            return self.t_flag[0]
        if sel == 10:
            return self.t_flag[1]
        if sel == 11:
            return self.rx_full
        if sel == 12:
            return 0 if self.tx_hfull else 1
        if sel == 13:
            if not self.tx_busy:
                return 0 if self.tx_hfull else 1
            # Still busy: true in the very cycle the last bit ends, so the program can act on that tick.
            if self.t_tick0 and not self.tx_ext and not self.tx_cnt and not self.tx_hfull:
                if self.tx_split and not self.tx_half:
                    return 0
                if self.tx_stuff and self.rl_cnt >= self.rl_n:
                    return 0
                return 1
            return 0
        if sel == 14:
            return self.match
        if sel == 15:
            return 1 if self.crc_n else 0
        chip = self.chip
        if sel == 16:
            return 1 if chip.fifo[0] else 0
        if sel == 17:
            return 1 if chip.fifo[3] else 0
        if sel == 18:
            return 1 if len(chip.fifo[1]) < isa.FIFO_DEPTH else 0
        if sel == 19:
            return 1 if len(chip.fifo[2]) < isa.FIFO_DEPTH else 0
        if sel == 20:
            return self.z
        if sel == 21:
            return self.c
        if sel == 22:
            return self.smp_edge
        if sel == 23:
            return 1 if self.smp_running else 0
        if sel == 24:
            return self.rl_err
        if sel == 25:
            return chip.sig & 1
        if sel == 26:
            return (chip.sig >> 1) & 1
        return 0

    # ---- engine -------------------------------------------------------------------------------------

    def _src_ready(self, src: int) -> bool:
        chip = self.chip
        if src == 4:
            return bool(chip.fifo[0]) and not chip.used & 1
        if src == 5:
            return bool(chip.fifo[3]) and not chip.used & 8
        if src == 6:
            return bool(self.rx_full)
        if src == 7 or src == 8:
            return not self.crc_n
        return True

    def _dst_ready(self, dst: int) -> bool:
        chip = self.chip
        if dst == 5:
            return len(chip.fifo[1]) < isa.FIFO_DEPTH and not chip.used & 2
        if dst == 6:
            return len(chip.fifo[2]) < isa.FIFO_DEPTH and not chip.used & 4
        if dst == 7:
            return not self.tx_hfull
        if 8 <= dst <= 10:
            return not self.crc_n
        return True

    def _read(self, src: int, pins: int) -> int:
        chip = self.chip
        if src < 4:
            return self.r[src]
        if src == 4:
            chip.used |= 1
            return chip.fifo[0].popleft()
        if src == 5:
            chip.used |= 8
            return chip.fifo[3].popleft()
        if src == 6:
            self.rx_full = 0
            return self.rx_cap
        if src == 7:
            return (self.crc ^ self.crc_xor) & M16
        if src == 8:
            return ((self.crc ^ self.crc_xor) >> 16) & M16
        if src == 9:
            return pins
        if src == 10:
            return self.t_cnt[0]
        if src == 11:
            return self.t_cnt[1]
        return 0

    def _queue(self, dst: int, value: int) -> None:
        if dst == 5:
            self.chip.used |= 2
        elif dst == 6:
            self.chip.used |= 4
        self.pend.append((dst, value))

    def engine(self, pins: int) -> None:
        if self.extra:
            self.extra = 0
            return
        if self.fetch:  # the synchronous SRAM read: the word is usable from the next cycle
            ir = self.mem[self.pc]
            self.op = ir >> 28
            self.inv = (ir >> 27) & 1
            self.sel = (ir >> 22) & 31
            self.pay = ir & 0x3FFFFF
            self.fetch = False
            return
        if self.trap_en and self.event(self.trap_sel, pins) != self.trap_inv:
            if 9 <= self.trap_sel <= 10 and not self.trap_inv:
                self.t_flag[self.trap_sel - 9] = 0
            self.tlr = self.pc
            self.pc = self.trap_tgt
            self.trap_en = False
            self.fetch = True
            return
        sel = self.sel
        cond = (self.event(sel, pins) if sel else 1) != self.inv
        op = self.op
        pay = self.pay
        if op == isa.BR:
            self.pc = (pay & 511) if cond else (self.pc + 1) & 511
            self.fetch = True
            return
        if not cond:
            return
        nxt = (self.pc + 1) & 511
        if op == isa.MOV:
            src = (pay >> 18) & 15
            da = (pay >> 11) & 31
            db = (pay >> 6) & 31
            if not (self._src_ready(src) and self._dst_ready(da) and self._dst_ready(db)):
                return
            value = self._read(src, pins)
            if pay & 0x20000:       # .hi on the source: take the high byte
                value >>= 8
            if pay & 0x10000:       # .hi on the destination: write only the high byte of a register
                da |= 0x100
            self._queue(da, value)
            if db:
                self._queue(db, value)
        elif op == isa.LDI:
            dst = (pay >> 16) & 31
            if not self._dst_ready(dst):
                return
            self._queue(dst, pay & M16)
        elif op == isa.ALU:
            fn = (pay >> 19) & 7
            rd = (pay >> 17) & 3
            a = self.r[rd]
            b = self.r[pay & 3] if pay & 0x10000 else pay & M16
            if fn == 0:
                res = a + b
                self.c = res >> 16
                res &= M16
            elif fn == 1 or fn == 6:
                res = (a - b) & M16
                self.c = 1 if a < b else 0
            elif fn == 2 or fn == 7:
                res = a & b
            elif fn == 3:
                res = a | b
            elif fn == 4:
                res = a ^ b
            else:
                res = a >> 1
                self.c = a & 1
            self.z = 1 if res == 0 else 0
            if fn < 6:
                self.r[rd] = res
        elif op == isa.ACT:
            self.strobes |= pay & 0x1FFF
        elif op == isa.PIN:
            chip = self.chip
            for pin in range(8):
                code = (pay >> (2 * pin)) & 3
                if code:
                    bit = 1 << pin
                    if code == 3:
                        chip.pin_oe &= ~bit
                    else:
                        chip.pin_oe |= bit
                        chip.pin_out = (chip.pin_out | bit) if code == 2 else (chip.pin_out & ~bit)
        elif op == isa.DJNZ:
            rn = (pay >> 9) & 3
            value = (self.r[rn] - 1) & M16
            self.r[rn] = value
            if value:
                nxt = pay & 511
            self.extra = 1
        elif op == isa.JMP:
            if pay & 0x200:
                self.lr = nxt
            nxt = pay & 511
        elif op == isa.RET:
            nxt = self.tlr if pay & 1 else self.lr
        elif op == isa.TRAP:
            self.trap_en = bool(pay & 0x8000)
            self.trap_inv = (pay >> 14) & 1
            self.trap_sel = (pay >> 9) & 31
            self.trap_tgt = pay & 511
        elif op == isa.HALT:
            self.run = False
            return
        if 9 <= sel <= 10 and not self.inv:  # waiting on a timer consumes its flag
            self.t_flag[sel - 9] = 0
        self.pc = nxt
        self.fetch = True

    # ---- writes that land at the end of the cycle ------------------------------------------------------

    def write(self, dst: int, value: int) -> None:
        value &= M16
        if dst & 0x100:
            index = (dst & 31) - 1
            if 0 <= index < 4:
                self.r[index] = (self.r[index] & 0xFF) | ((value & 0xFF) << 8)
            return
        if dst == 0:
            return
        if dst <= 4:
            self.r[dst - 1] = value
        elif dst == 5:
            self.chip.fifo[1].append(value & 0xFF)
        elif dst == 6:
            self.chip.fifo[2].append(value & 0xFF)
        elif dst == 7:
            self.tx_hold = value
            self.tx_hfull = 1
        elif dst == 8:
            self.crc_data = value & 0xFF
            self.crc_n = 8
        elif dst == 9:
            self.crc = (self.crc & 0xFFFF0000) | value
        elif dst == 10:
            self.crc = (self.crc & M16) | (value << 16)
        elif dst == 11 or dst == 12:
            i = dst - 11
            self.t_lim[i] = value if value > 2 else 2
            self.t_cnt[i] = 2
            self.t_run[i] = True
            self.t_flag[i] = 0
            if i == 0:
                self.t_tick0 = 0
        elif dst == isa.D_SMPPERIOD:
            self.smp_period = value if value > 2 else 2
        elif dst == isa.D_SMPPHASE:
            self.smp_phase = value if value > 2 else 2
        elif dst == isa.D_SMPCFG:
            self.smp_clk = value & 7
            self.smp_data = (value >> 3) & 7
            self.smp_edgemode = (value >> 6) & 3
            self.smp_mode = (value >> 8) & 3
            self.smp_inv = (value >> 10) & 1
            self.smp_nrzi = (value >> 11) & 1
        elif dst == isa.D_RXCFG:
            self.rx_nbits = (value & 15) + 1
            self.rx_msb = (value >> 4) & 1
            self.rx_clock = (value >> 5) & 3
            self.rx_destuff = (value >> 7) & 1
        elif dst == isa.D_TXCFG:
            self.tx_nbits = (value & 15) + 1
            self.tx_msb = (value >> 4) & 1
            self.tx_code = (value >> 5) & 3
            self.tx_inv = (value >> 7) & 1
            self.tx_split = (value >> 8) & 1
            self.tx_stuff = (value >> 9) & 1
            self.tx_init = (value >> 10) & 1
            self.tx_clkinv = (value >> 11) & 1
            self.tx_ext = (value >> 12) & 1
            if not self.tx_busy:
                self.tx_line = self.tx_init
        elif dst == isa.D_RLN:
            self.rl_n = value & 15
        elif dst == isa.D_POLYLO:
            self.crc_poly = (self.crc_poly & 0xFFFF0000) | value
        elif dst == isa.D_POLYHI:
            self.crc_poly = (self.crc_poly & M16) | (value << 16)
        elif dst == isa.D_CRCCFG:
            self.crc_reflect = value & 1
            self.crc_xor = M32 if value & 2 else 0
        elif dst == isa.D_MVAL:
            self.m_val = value
        elif dst == isa.D_MMASK:
            self.m_mask = value
        elif dst == isa.D_MCFG:
            self.m_src = value & 1
        elif dst == isa.D_TMRCFG:
            self.t_auto = [value & 1, (value >> 8) & 1]
            self.t_rsel = [(value >> 1) & 31, (value >> 9) & 31]
            self.t_rinv = [(value >> 6) & 1, (value >> 14) & 1]
        elif dst == isa.D_PINSRC:
            self.m_tx = self.m_ntx = self.m_clk = 0
            for pin in range(8):
                code = (value >> (2 * pin)) & 3
                if code == 1:
                    self.m_tx |= 1 << pin
                elif code == 2:
                    self.m_ntx |= 1 << pin
                elif code == 3:
                    self.m_clk |= 1 << pin
            self.m_any = self.m_tx | self.m_ntx | self.m_clk
        elif dst == isa.D_PINOD:
            self.od = value & 0xFF

    # ---- helpers ------------------------------------------------------------------------------------

    def rx_sample(self, pins: int) -> None:
        """One bit into the receive pipeline: line decode, optional destuff, shift, capture."""
        level = ((pins >> self.smp_data) & 1) ^ self.smp_inv
        if self.smp_nrzi:
            bit = 1 ^ level ^ self.rx_prev
            self.rx_prev = level
        else:
            bit = level
        if self.rx_destuff:
            if self.rl_cnt >= self.rl_n:    # this is the stuffed bit: drop it
                self.rl_cnt = 0
                if bit:
                    self.rl_err = 1
                return
            self.rl_cnt = self.rl_cnt + 1 if bit else 0
        n = self.rx_nbits
        if self.rx_msb:
            live = ((self.rx_live << 1) | bit) & M16
        else:
            live = ((self.rx_live & ((1 << n) - 1)) >> 1) | (bit << (n - 1))
        self.rx_live = live
        self.rx_cnt += 1
        if self.rx_cnt >= n:
            self.rx_cap = live & ((1 << n) - 1)
            self.rx_full = 1
            self.rx_cnt = 0

    def tx_tick(self, pins: int) -> None:
        """One tick of the transmit pipeline: shifter, optional stuffing, line coder."""
        if self.tx_split and self.tx_busy and not self.tx_half:
            self.tx_half = 1                # the middle of a bit
            if self.tx_code == 2:
                self.tx_line = self.tx_bit ^ self.tx_inv
            if self.rx_clock == 1:
                self.rx_sample(pins)
            return
        if self.tx_busy and self.rx_clock == 2:
            self.rx_sample(pins)            # the end of a bit: the latest moment the answer is still on the pin
        self.tx_half = 0
        if self.tx_busy and self.tx_stuff and self.rl_cnt >= self.rl_n:
            bit = 0
            self.rl_cnt = 0
        else:
            if not self.tx_cnt:
                if not self.tx_hfull:
                    self.tx_busy = 0
                    self.tx_line = self.tx_init
                    return
                self.tx_sh = self.tx_hold
                self.tx_cnt = self.tx_nbits
                self.tx_hfull = 0
                if not self.tx_busy:
                    self.tx_busy = 1
                    self.tx_line = self.tx_init
                    self.rl_cnt = 0
            if self.tx_msb:
                bit = (self.tx_sh >> (self.tx_nbits - 1)) & 1
                self.tx_sh = (self.tx_sh << 1) & M16
            else:
                bit = self.tx_sh & 1
                self.tx_sh >>= 1
            self.tx_cnt -= 1
            if self.tx_stuff:
                self.rl_cnt = self.rl_cnt + 1 if bit else 0
        self.tx_bit = bit
        code = self.tx_code
        if code == 0:
            self.tx_line = bit ^ self.tx_inv
        elif code == 1:
            if not bit ^ self.tx_inv:
                self.tx_line ^= 1
        else:
            self.tx_line = 1 ^ bit ^ self.tx_inv

    def helpers(self, pins: int) -> None:
        tick0 = self.t_tick0
        self.t_tick0 = 0
        # Timers: count up to the period; a retrigger event restarts the count.
        t_run = self.t_run
        if t_run[0] or t_run[1]:
            for i in (0, 1):
                if t_run[i]:
                    rsel = self.t_rsel[i]
                    if rsel and self.event(rsel, pins) != self.t_rinv[i]:
                        self.t_cnt[i] = 1
                    elif self.t_cnt[i] == self.t_lim[i]:
                        self.t_flag[i] = 1
                        if i == 0:
                            self.t_tick0 = 1
                        if self.t_auto[i]:
                            self.t_cnt[i] = 1
                        else:
                            t_run[i] = False
                    else:
                        self.t_cnt[i] = (self.t_cnt[i] + 1) & M16
        # Matcher: a registered compare of the live receive bits (or the pins) with a masked value.
        if self.m_mask:
            self.match = 0 if ((pins if self.m_src else self.rx_live) ^ self.m_val) & self.m_mask else 1
        # Sampler: track edges of the clock pin and say when to sample the data pin.
        cur = (pins >> self.smp_clk) & 1
        tick = self.smp_tick
        self.smp_tick = 0
        self.smp_edge = 0
        armed = self.smp_armed
        if armed or self.smp_running or tick:
            mode = self.smp_mode
            accept = armed and cur != self.smp_last and (
                not self.smp_edgemode or (cur == 0) == (self.smp_edgemode == 1))
            if tick:
                if not self.rx_clock:
                    self.rx_sample(pins)
                if mode == 1:               # gate: one tick per edge, then listen for the next edge
                    self.smp_armed = True
                    self.smp_running = False
            if accept:
                self.smp_cnt = self.smp_phase
                self.smp_running = True
                self.smp_armed = mode == 0
                self.smp_edge = 1
            elif self.smp_running:
                if tick:
                    self.smp_cnt = self.smp_period
                else:
                    if self.smp_cnt == 2:
                        self.smp_tick = 1
                    self.smp_cnt -= 1
            elif armed and self.smp_nrzi and not tick:
                self.rx_prev = ((pins >> self.smp_data) & 1) ^ self.smp_inv   # remember the idle level
        self.smp_last = cur
        # Strobes the engine raised in the previous cycle.
        st = self.strobes
        if st:
            self.strobes = 0
            if st & 2:
                self.tx_busy = self.tx_hfull = self.tx_cnt = self.tx_half = 0
                self.tx_line = self.tx_init
            if st & 1:
                self.tx_tick(pins)
            if st & 8:
                self.rx_live = self.rx_cnt = self.rx_full = 0
            if st & 4:
                self.rx_sample(pins)
            if st & 16:
                self.smp_armed = True
                self.smp_running = False
                self.smp_tick = 0
            if st & 32:
                self.smp_armed = self.smp_running = False
                self.smp_tick = 0
            if st & 64:
                self.rl_cnt = self.rl_err = 0
            if st & 128:
                t_run[0] = False
                self.t_flag[0] = self.t_tick0 = 0
            if st & 256:
                t_run[1] = False
                self.t_flag[1] = 0
            if st & 0x1E00:
                chip = self.chip
                if st & 0x200:
                    chip.sig |= 1
                if st & 0x400:
                    chip.sig &= ~1
                if st & 0x800:
                    chip.sig |= 2
                if st & 0x1000:
                    chip.sig &= ~2
        # Transmitter, clocked by timer 0.
        if tick0 and not self.tx_ext:
            self.tx_tick(pins)
        # CRC: one bit per cycle.
        if self.crc_n:
            self.crc_n -= 1
            if self.crc_reflect:
                feedback = (self.crc ^ self.crc_data) & 1
                self.crc_data >>= 1
                self.crc >>= 1
            else:
                feedback = ((self.crc >> 31) ^ (self.crc_data >> 7)) & 1
                self.crc_data = (self.crc_data << 1) & 0xFF
                self.crc = (self.crc << 1) & M32
            if feedback:
                self.crc ^= self.crc_poly
        # The engine's writes land last.
        if self.pend:
            for dst, value in self.pend:
                self.write(dst, value)
            self.pend = []


class EventChip(Chip):
    name = "event"

    def __init__(self) -> None:
        self.lanes = [Lane(self, i) for i in range(isa.N_LANES)]
        self.reset()

    def reset(self) -> None:
        for lane in self.lanes:
            lane.reset()
        self.fifo: List[Deque[int]] = [deque() for _ in range(4)]
        self.pin_out = 0
        self.pin_oe = 0
        self.sig = 0
        self.used = 0

    def load(self, image: bytes) -> None:
        """The loader's job: copy each lane's words into its program memory and start it at address 0."""
        counts = [image[0] | image[1] << 8, image[2] | image[3] << 8]
        pos = 4
        for lane, count in zip(self.lanes, counts):
            for i in range(count):
                lane.mem[i] = int.from_bytes(image[pos:pos + 4], "little")
                pos += 4
            lane.run = lane.live = count > 0

    def step(self, pins: int) -> Tuple[int, int]:
        self.used = 0
        l0, l1 = self.lanes
        if l0.run:
            l0.engine(pins)
        if l1.run:
            l1.engine(pins)
        if l0.live:
            l0.helpers(pins)
        if l1.live:
            l1.helpers(pins)
        out = self.pin_out
        oe = self.pin_oe
        for lane in (l1, l0):   # lane 0 wins a pin both lanes claim
            if lane.tx_busy and lane.m_any:
                mask = lane.m_any
                level = (lane.m_tx if lane.tx_line else lane.m_ntx) | (
                    lane.m_clk if lane.tx_half ^ lane.tx_clkinv else 0)
                out = (out & ~mask) | level
                oe |= mask
        od = l0.od | l1.od
        if od:
            oe &= ~(od & out)   # an open-drain pin never drives high
        return out, oe

    def host_can_push(self, ch: int) -> bool:
        return (ch == 0 or ch == 3) and len(self.fifo[ch]) < isa.FIFO_DEPTH

    def host_push(self, ch: int, byte: int) -> None:
        self.fifo[ch].append(byte & 0xFF)

    def host_pop(self, ch: int) -> Optional[int]:
        if (ch == 1 or ch == 2) and self.fifo[ch]:
            return self.fifo[ch].popleft()
        return None

    def budget(self) -> Dict[str, object]:
        lane = sum(LANE_FLOPS.values())
        flops = isa.N_LANES * lane + sum(SHARED_FLOPS.values())
        return {
            "flops": flops,
            "sram_bits": SRAM_BITS,
            "gates_estimate": isa.N_LANES * sum(LANE_GATES.values()) + sum(SHARED_GATES.values()),
            "engines": isa.N_LANES,
            "notes": "2 lanes x %d flops + %d shared; program memory is one 512x32 SRAM per lane; "
                     "each lane runs one protocol, full duplex included" % (lane, sum(SHARED_FLOPS.values())),
        }
