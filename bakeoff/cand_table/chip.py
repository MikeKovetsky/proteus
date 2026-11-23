"""Cycle-accurate model of the table machine.

Every clock, one machine takes one step: the entry its state selects is on the SRAM outputs, two selected
condition flags are OR-ed into the low bits of the entry's `base` to form the next address, and the entry's
micro-operations update that machine's registers. The SRAM address register is the state register, so the
read that follows a decision is already under way on the next clock: one decision per clock, with the
synchronous read modelled explicitly (`q_addr` is what the SRAM latched on the previous clock).

With `nm` machines, they take turns clock by clock (one read port), so each machine decides every `nm` clocks
and its timer counts its own turns.

Logic depth per clock, to the bake-off's rule:
  next address  flags are flops (each zero flag is computed beside the register it describes, and the
                LFSR compare is registered a turn late) -> 16:1 select -> 2-input OR -> 2:1 mux with the
                link register -> SRAM address. `S == imm` is one 8-bit compare in front of that select.
  datapath      one 16-bit increment or decrement (T, N), or one LFSR step, or one byte through the bus
                mux, each behind a 2:1 or 4:1 mux. Nothing chains two of them.
  pins          a pin operation may be gated by condition A: select -> AND -> pad register.
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

from ..harness.chip import Chip
from . import entry as E

FIFO_DEPTH = 4


class Machine:
    """One machine's registers. The logic that updates them is shared by all machines."""

    __slots__ = ("state", "link", "T", "N", "K", "J", "S", "R", "crc", "crc_ok", "prev_a", "prev_b",
                 "out", "inp", "msb", "poly", "init", "match", "in_b_half", "edge_b", "in_d_en", "in_d")

    def __init__(self, index: int) -> None:
        self.state = index  # machine m starts in table entry m
        self.link = 0       # return address of the one subroutine level
        self.T = 0          # 16-bit down-timer, stops at zero
        self.N = 0          # 16-bit up/down counter
        self.K = 0          # 4-bit down-counters
        self.J = 0
        self.S = 0          # 8-bit shifter
        self.R = 0          # 8-bit holding register
        self.crc = 0        # 32-bit LFSR, right-shifting (reflected polynomials)
        self.crc_ok = 0     # LFSR == match, registered: one turn behind the LFSR
        self.prev_a = 1     # input A one turn ago, for `edge`
        self.prev_b = 1     # input B one turn ago, for `edge_b`
        self.out = [0, 0, 0, 0]
        self.inp = [0, 0, 0]
        self.msb = 0
        self.poly = 0
        self.init = 0
        self.match = 0
        self.in_b_half = 0
        self.edge_b = 0
        self.in_d_en = 0
        self.in_d = 0


class TableChip(Chip):
    name = "table"

    def __init__(self) -> None:
        self.reset()

    # ---- reset and load -------------------------------------------------------------------------------

    def reset(self) -> None:
        self.ram_a = [0] * E.DEPTH   # SRAM macro 1: word A of every entry
        self.ram_b = [0] * E.DEPTH   # SRAM macro 2: word B
        self._decoded: List[Optional[tuple]] = [None] * E.DEPTH  # simulator speed-up only: unpacked fields
        self.nm = 1
        self.mach = [Machine(i) for i in range(E.MAX_MACHINES)]
        self.out = 0                 # pad output register
        self.oe = 0                  # pad output-enable register
        self.fifo: List[Deque[int]] = [deque() for _ in range(4)]
        self.slot = 0                # whose turn it is
        self.q_addr = 0              # the address the SRAMs latched on the previous clock
        self.q_valid = False         # false until the first read after reset has happened
        self.loaded = False
        self.overflow = 0            # pushes dropped because a FIFO was full
        self.tcam: List[Tuple[int, int, int]] = []  # v2: up to 16 (val, mask, cls) rules

    def load(self, image: bytes) -> None:
        """The loader: copies the configuration into flops and the entries into the two SRAMs, in order."""
        configs, words, tcam = E.parse_image_v2(image)
        self.nm = len(configs)
        for m, cfg in zip(self.mach, configs):
            m.out, m.inp, m.msb = list(cfg.out), list(cfg.inp), cfg.msb
            m.poly, m.init, m.match = cfg.poly, cfg.init, cfg.match
            m.in_b_half, m.edge_b = cfg.in_b_half, cfg.edge_b
            m.in_d_en, m.in_d = cfg.in_d_en, cfg.in_d
        self.tcam = list(tcam[:E.MAX_TCAM])
        for addr, (a, b) in enumerate(words):
            self.ram_a[addr] = a
            self.ram_b[addr] = b
        self._decoded = [None] * E.DEPTH  # the speed-up cache must never outlive the entries it unpacked
        self.loaded = True

    # ---- the host link --------------------------------------------------------------------------------

    def host_can_push(self, ch: int) -> bool:
        return ch in (0, 3) and len(self.fifo[ch]) < FIFO_DEPTH

    def host_push(self, ch: int, byte: int) -> None:
        self.fifo[ch].append(byte & 0xFF)

    def host_pop(self, ch: int) -> Optional[int]:
        if ch in (1, 2) and self.fifo[ch]:
            return self.fifo[ch].popleft()
        return None

    # ---- one clock ------------------------------------------------------------------------------------

    def _decode(self, addr: int) -> tuple:
        f = E.unpack(self.ram_a[addr], self.ram_b[addr])
        # `quiet` marks an entry with no micro-operation, so the simulator can skip the datapath code.
        quiet = not any(f[i] for i in (4, 6, 11, 12, 13, 14, 15, 16, 17))
        d = f + (quiet,)
        self._decoded[addr] = d
        return d

    def _tcam_cls(self, s: int) -> int:
        for val, mask, cls in self.tcam:
            if cls and ((s & mask) == (val & mask)):
                return cls
        return 0

    def _cond(self, m: Machine, c: int, pins: int, a: int, b: int, d: int, imm: int) -> int:
        if c == 0:
            return 0
        if c == 5:
            return 1 if m.T == 0 else 0
        if c == 1:
            return a
        if c == 4:
            return ((a ^ m.prev_a) | (b ^ m.prev_b)) if m.edge_b else (a ^ m.prev_a)
        if c == 2:
            return b
        if c == 3:
            return (pins >> m.inp[2]) & 1
        if c == 6:
            return 1 if m.K == 0 else 0
        if c == 7:
            return 1 if m.J == 0 else 0
        if c == 8:
            return 1 if m.N == 0 else 0
        if c == 9:
            return d
        if c == 10:
            return m.crc_ok
        if c == 11:
            return 1 if self.fifo[0] else 0
        if c == 12:
            return ((pins >> m.in_d) & 1) if m.in_d_en else (1 if self.fifo[3] else 0)
        if c == 13:
            return 1 if len(self.fifo[1]) < FIFO_DEPTH else 0
        if c == 14:
            return 1 if len(self.fifo[2]) < FIFO_DEPTH else 0
        return 1 if m.S == (imm & 0xFF) else 0

    def step(self, pins: int, pins_half: Optional[int] = None) -> Tuple[int, int]:
        if not self.loaded:
            return 0, 0
        if pins_half is None:
            pins_half = pins
        mi = self.slot
        nxt = mi + 1
        if nxt >= self.nm:
            nxt = 0
        self.slot = nxt

        if self.q_valid:
            m = self.mach[mi]
            e = self._decoded[self.q_addr] or self._decode(self.q_addr)
            (base, ca, cb, p0, p0op, p1, p1op, gate, dsel, din, imm, src, dst, shift, crcop, nop, kj, tload,
             quiet) = e

            # Everything below reads the registers as they stood before this clock.
            a = (pins >> m.inp[0]) & 1
            b = ((pins_half if m.in_b_half else pins) >> m.inp[1]) & 1
            S = m.S
            if dsel:
                d = m.crc & 1
            elif m.msb:
                d = S >> 7
            else:
                d = S & 1
            va = self._cond(m, ca, pins, a, b, d, imm) if ca else 0
            vb = self._cond(m, cb, pins, a, b, d, imm) if cb else 0

            # The 32-bit compare is registered, so the `crc` flag describes the LFSR one turn ago.
            m.crc_ok = 1 if m.crc == m.match else 0
            if quiet:
                if m.T:
                    m.T -= 1
            else:
                self._datapath(m, e, a, b, d, va)
            m.prev_a = a
            m.prev_b = b
            if nop == E.N_RETURN:
                m.state = m.link
            elif gate == E.GATE_DISPATCH:
                mode = ca & 3
                if mode == E.DISP_SLICE2:
                    m.state = (base & 0x1FC) | ((S >> (cb & 7)) & 3)
                elif mode == E.DISP_NIBBLE:
                    m.state = (base & 0x1F0) | ((S >> 4) if (cb & 1) else (S & 15))
                elif mode == E.DISP_TCAM8:
                    m.state = (base & 0x1F8) | (self._tcam_cls(S) & 7)
                elif cb & 1:
                    m.state = (base & 0x1FC) | (self._tcam_cls(S) & 3)
                else:
                    m.state = (base & 0x1F0) | (self._tcam_cls(S) & 15)
            else:
                m.state = base | va | (vb << 1)

        # The SRAMs latch the address of whoever steps next. With one machine that is the state just chosen.
        self.q_addr = self.mach[nxt].state
        self.q_valid = True
        return self.out, self.oe

    def _datapath(self, m: Machine, e: tuple, a: int, b: int, d: int, va: int) -> None:
        (base, ca, cb, p0, p0op, p1, p1op, gate, dsel, din, imm, src, dst, shift, crcop, nop, kj, tload,
         quiet) = e

        # Pin operations: the only micro-operations a condition can gate (a mux, no adder behind it).
        if (p0op or p1op) and (gate == 0 or (gate == 1 and va) or (gate == 2 and not va)):
            for sel, op in ((p0, p0op), (p1, p1op)):
                if not op:
                    continue
                bit = 1 << m.out[sel]
                if op == E.P_LOW:
                    self.out &= ~bit
                    self.oe |= bit
                elif op == E.P_HIGH:
                    self.out |= bit
                    self.oe |= bit
                elif op == E.P_RELEASE:
                    self.oe &= ~bit
                elif op == E.P_DATA:
                    self.out = (self.out | bit) if d else (self.out & ~bit)
                    self.oe |= bit
                elif op == E.P_NDATA:
                    self.out = (self.out & ~bit) if d else (self.out | bit)
                    self.oe |= bit
                elif op == E.P_TOGGLE:
                    self.out ^= bit
                    self.oe |= bit
                else:  # open drain: a 1 releases the pin, a 0 pulls it low
                    self.out &= ~bit
                    self.oe = (self.oe & ~bit) if d else (self.oe | bit)

        # The byte bus: one source, one destination.
        S = m.S
        N = m.N
        value = 0
        if dst or src == E.SRC_POP0 or src == E.SRC_POP3:
            if src == E.SRC_IMM:
                value = imm & 0xFF
            elif src == E.SRC_POP0:
                value = self.fifo[0].popleft() if self.fifo[0] else 0
            elif src == E.SRC_POP3:
                value = self.fifo[3].popleft() if self.fifo[3] else 0
            elif src == E.SRC_S:
                value = S
            elif src == E.SRC_R:
                value = m.R
            elif src == E.SRC_NLO:
                value = N & 0xFF
            elif src == E.SRC_NHI:
                value = N >> 8

        # Serial input, shifter and LFSR.
        if din == E.DIN_A:
            bit_in = a
        elif din == E.DIN_B:
            bit_in = b
        else:
            bit_in = din
        if shift:
            S = ((S << 1) & 0xFF) | bit_in if m.msb else (S >> 1) | (bit_in << 7)
        if crcop:
            crc = m.crc
            if crcop == E.CRC_ZERO:
                crc = 0
            elif crcop == E.CRC_INIT:
                crc = m.init
            elif crcop == E.CRC_SHIFT:
                crc >>= 1
            else:
                feedback = (crc ^ (d if crcop == E.CRC_STEP_D else bit_in)) & 1
                crc >>= 1
                if feedback:
                    crc ^= m.poly
            m.crc = crc

        # Counters.
        if nop == 1:
            N = (N - 1) & 0xFFFF
        elif nop == 2:
            N = (N + 1) & 0xFFFF
        if kj:
            k_op, j_op = divmod(kj, 3)
            if k_op == E.DEC:
                m.K = (m.K - 1) & 15
            elif k_op == E.LOAD:
                m.K = (imm >> 8) & 15
            if j_op == E.DEC:
                m.J = (m.J - 1) & 15
            elif j_op == E.LOAD:
                m.J = (imm >> 12) & 15
        if tload:
            m.T = imm
        elif m.T:
            m.T -= 1

        # Bus destination.
        if dst == E.DST_S:
            S = value
        elif dst == E.DST_R:
            m.R = value
        elif dst == E.DST_N:
            N = value
        elif dst == E.DST_NHI:
            N = (N & 0xFF) | (value << 8)
        elif dst == E.DST_LINK:
            m.link = imm & 0x1FF  # a call: nine bits straight from the immediate, not through the byte bus
        elif dst == E.DST_PUSH1 or dst == E.DST_PUSH2:
            queue = self.fifo[1 if dst == E.DST_PUSH1 else 2]
            if len(queue) < FIFO_DEPTH:
                queue.append(value)
            else:
                self.overflow += 1
        m.S = S
        m.N = N

    # ---- cost -----------------------------------------------------------------------------------------

    def budget(self) -> Dict[str, object]:
        per_machine = {
            "state (SRAM address shadow)": 9, "link (return address)": 9,
            "T timer": 16, "N counter": 16, "K": 4, "J": 4, "S shifter": 8, "R register": 8,
            "LFSR": 32, "LFSR polynomial": 32, "LFSR init": 32, "LFSR match": 32,
            "flags tz kz jz nz crc, previous A": 6,
            "pin map (4 out, 3 in) and bit order": 22,
        }
        shared = {
            "host FIFOs 4 x 4 bytes": 4 * FIFO_DEPTH * 8,
            "FIFO pointers and counts": 4 * 7,
            "pad out and oe registers": 16,
            "input synchronisers (2 x 8, in the harness)": 16,
            "turn counter, machine count, read-valid, run": 6,
            "loader (word assembly, address, byte count)": 48,
            "host link (strobe synchroniser, nibble assembly, channel)": 40,
        }
        machine_bits = sum(per_machine.values())
        flops = E.MAX_MACHINES * machine_bits + sum(shared.values())
        return {
            "flops": flops,
            "sram_bits": 2 * E.DEPTH * 32,
            "gates_estimate": 2600,
            "engines": E.MAX_MACHINES,
            "notes": (
                "%d flops per machine x %d machines + %d shared. One machine decides every clock (60 MHz), "
                "two every other clock (30 MHz each), four every fourth (15 MHz each). Datapath logic is "
                "shared between machines; only the registers are repeated. The table is 512 entries of 64 "
                "bits in two 512x32 macros. Gates are an estimate of combinational cells, not synthesised. "
                "Not yet checked in synthesis: the loop SRAM output -> condition select -> SRAM address must "
                "close in one 16.7 ns clock." % (machine_bits, E.MAX_MACHINES, sum(shared.values()))
            ),
        }
