#!/usr/bin/env python3
# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Interactive Judge-Mode TUI for Proteus Inline Hardware Pre-Trade Risk Gate + Qubx OME.

Drives the real unmodified Proteus RTL v2 Verilog at 60 MHz via Verilator (zero hardware needed).
Run:
    make -C examples/qubx_risk_gate demo             # interactive keyboard dashboard
    python3 examples/qubx_risk_gate/demo_tui.py --showcase   # scripted showcase + Port-3 trace
"""
from __future__ import annotations

import argparse
import os
import random
import select
import sys
import termios
import time
import tty
from collections import deque
from pathlib import Path
from typing import Deque, List, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from qubx_gate import (  # noqa: E402
    NS_PER_CLK,
    OP_CANCEL,
    OP_HEARTBEAT,
    OP_NEW_ORDER,
    OrderFrame,
    ProteusHardwareGate,
    SimulatedOME,
    render_port3_waveform,
)

# ANSI palette
RST = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"


class DemoState:
    def __init__(self, gate: ProteusHardwareGate):
        self.gate = gate
        self.ome = SimulatedOME()
        self.armed = True
        self.rogue = False
        self.alert_rl = False
        self.alert_kill = False
        self.total_sent = 0
        self.passed = 0
        self.dropped_rl = 0
        self.dropped_kill = 0
        self.dropped_ff = 0
        self.dropped_op = 0
        self.new_latencies_ns: List[float] = []
        self.cxl_latencies_ns: List[float] = []
        self.last_kill_ns: float | None = None
        self.last_trace: List[Tuple[int, int]] = []
        self.last_trace_title: str = "Press [N], [M], or [R] to capture live Port-3 hardware trace"
        self.log: Deque[str] = deque(maxlen=10)
        self.seq = 0

    def send(self, frame: OrderFrame, *, kill_mid_tx_clks: int = -1, arm_trace: int = 0, note: str = "") -> None:
        self.seq += 1
        self.total_sent += 1
        res = self.gate.submit(frame, kill_mid_tx_clks=kill_mid_tx_clks, arm_trace=arm_trace)
        self.alert_rl = res.alert_rl
        self.alert_kill = res.alert_kill
        if kill_mid_tx_clks >= 0:
            self.armed = False

        if res.trace:
            self.last_trace = res.trace
            if kill_mid_tx_clks >= 0:
                self.last_trace_title = (
                    f"MID-PACKET KILL ZOOM: P4 fell mid-TX -> cut in {res.kill_resp_clks} clks "
                    f"({res.kill_resp_ns:.1f} ns @ 60 MHz)!"
                )
            else:
                self.last_trace_title = (
                    f"Last Captured Frame #{self.seq} ({res.verdict}, wire-to-wire = {res.lat_ns:.1f} ns)"
                    if res.lat_ns is not None
                    else f"Last Captured Frame #{self.seq} ({res.verdict})"
                )

        if res.passed:
            self.passed += 1
            assert res.forwarded is not None
            fill_str = self.ome.on_wire_frame(res.forwarded)
            if res.lat_ns is not None:
                if frame.opcode == OP_NEW_ORDER:
                    self.new_latencies_ns.append(res.lat_ns)
                else:
                    self.cxl_latencies_ns.append(res.lat_ns)
            tag = f"{GREEN}PASS{RST}" if frame.opcode == OP_NEW_ORDER else f"{CYAN}BYPASS{RST}"
            lat_s = f"{res.lat_ns:5.1f}ns" if res.lat_ns is not None else "   --  "
            self.log.append(f"#{self.seq:03d} [{tag}] {frame.label()} -> {lat_s} | {fill_str}")
        else:
            if 0xE1 in res.audits:
                self.dropped_rl += 1
                badge = f"{YELLOW}THROTTLE{RST}"
            elif 0xE2 in res.audits:
                self.dropped_kill += 1
                badge = f"{RED}KILLED  {RST}"
                if res.kill_resp_ns is not None:
                    self.last_kill_ns = res.kill_resp_ns
            elif 0xE4 in res.audits:
                self.dropped_ff += 1
                badge = f"{MAGENTA}FAT-FNGR{RST}"
            else:
                self.dropped_op += 1
                badge = f"{RED}BAD-OP  {RST}"
            extra = f" (cut in {res.kill_resp_ns:.1f}ns!)" if res.kill_resp_ns is not None else (f" {note}" if note else "")
            self.log.append(f"#{self.seq:03d} [{badge}] {frame.label()} -> {RED}BLOCKED ON WIRE{RST}{extra}")

    def toggle_kill(self) -> None:
        self.armed = not self.armed
        st = self.gate.set_kill_switch(self.armed)
        self.alert_kill = bool(st["alert_kill"])
        if not self.armed:
            self.rogue = False
            self.log.append(f"{RED}{BOLD}>>> PHYSICAL KILL-SWITCH P4 PULLED LOW — ALL TX CUT OFF <<<{RST}")
        else:
            self.log.append(f"{GREEN}{BOLD}>>> PHYSICAL KILL-SWITCH P4 RE-ARMED — TRADING RESUMED <<<{RST}")

    def render(self, interactive: bool = True) -> str:
        out: List[str] = []
        if interactive:
            out.append("\033[2J\033[H")
        out.append(f"{BOLD}{CYAN}╔══════════════════════════════════════════════════════════════════════════════╗{RST}")
        out.append(f"{BOLD}{CYAN}║  PROTEUS RTL v2 × QUBX — 60 MHz Silicon Pre-Trade Risk Gate & Kill-Switch   ║{RST}")
        out.append(f"{BOLD}{CYAN}╚══════════════════════════════════════════════════════════════════════════════╝{RST}")

        # Hardware LED bar (mirrors Tang Nano 9K LED0..LED5!)
        led_run = f"{GREEN}● RUN{RST}"
        led_rl = f"{YELLOW}● RATE-LIMIT{RST}" if self.alert_rl else f"{DIM}○ RATE-LIMIT{RST}"
        led_kill = f"{RED}{BOLD}● KILL-SWITCH{RST}" if not self.armed or self.alert_kill else f"{DIM}○ KILL-SWITCH{RST}"
        led_rogue = f"{RED}● ROGUE ALGO{RST}" if self.rogue else f"{DIM}○ ROGUE ALGO{RST}"
        out.append(f"  Board LEDs: {led_run}   {led_rl}   {led_kill}   {led_rogue}")
        out.append(
            f"  Silicon:    {WHITE}127/512 SRAM entries (24.8%){RST} | "
            f"{WHITE}2 Concurrent Machines (risk_gate + rl_timer){RST} | {WHITE}1-Clock TCAM{RST}"
        )
        out.append("")

        # Latency & OME stats (tracked per path — both are 0.0 ns jitter in silicon!)
        parts: List[str] = []
        if self.new_latencies_ns:
            s_lat = sorted(self.new_latencies_ns)
            p50 = s_lat[len(s_lat) // 2]
            jit = max(s_lat) - min(s_lat)
            parts.append(f"{GREEN}NewOrder: p50=p99={p50:.1f}ns (jitter {jit:.1f}ns){RST}")
        if self.cxl_latencies_ns:
            s_cxl = sorted(self.cxl_latencies_ns)
            p50 = s_cxl[len(s_cxl) // 2]
            jit = max(s_cxl) - min(s_cxl)
            parts.append(f"{CYAN}Cancel: {p50:.1f}ns (jitter {jit:.1f}ns){RST}")
        lat_line = "  |  ".join(parts) if parts else f"{DIM}(no approved frames yet){RST}"

        kill_s = f"{RED}{self.last_kill_ns:.1f} ns{RST}" if self.last_kill_ns is not None else f"{DIM}--{RST}"
        out.append(f"  {BOLD}Wire-to-Wire Latency:{RST} {lat_line}")
        out.append(
            f"  {BOLD}Qubx OME Fills:{RST} {GREEN}{self.ome.filled_orders}{RST} orders "
            f"(${self.ome.notional_usd:,.0f} notional) | "
            f"{CYAN}Cancels:{RST} {self.ome.cancelled_orders} | "
            f"{YELLOW}RL Drops:{RST} {self.dropped_rl} | "
            f"{MAGENTA}FatFinger:{RST} {self.dropped_ff} | "
            f"{RED}Killed:{RST} {self.dropped_kill} (cutoff: {kill_s})"
        )
        out.append("")

        # Order stream log
        out.append(f"  {BOLD}Live Wire Order Feed (P0 RX_IN -> Proteus Silicon -> P1 TX_OUT):{RST}")
        if not self.log:
            out.append(f"  {DIM}  (press [N] for NewOrder, [R] for Rogue Flood, [M] for Mid-Packet Kill){RST}")
        for entry in self.log:
            out.append(f"    {entry}")
        out.append("")

        # Port-3 Hardware Logic Analyzer Waveform
        out.append(f"  {BOLD}{CYAN}{self.last_trace_title}{RST}")
        for wf_line in render_port3_waveform(self.last_trace, max_cols=62):
            out.append(f"  {wf_line}")
        out.append("")

        if interactive:
            out.append(
                f"  {BOLD}Keys:{RST} {GREEN}[N]{RST} NewOrder  {CYAN}[X]{RST} Cancel(Bypass)  "
                f"{MAGENTA}[F]{RST} FatFinger  {YELLOW}[R]{RST} RogueFlood  "
                f"{RED}[K]{RST} KillWire(P4)  {RED}[M]{RST} MidTxKill  [W] +420us  [Q] Quit"
            )
        return "\n".join(out)


def run_showcase(gate: ProteusHardwareGate) -> None:
    st = DemoState(gate)
    # 1. Two clean normal orders (0 ns jitter!)
    st.send(OrderFrame(OP_NEW_ORDER, 1, 130, 10), arm_trace=1)
    st.send(OrderFrame(OP_NEW_ORDER, 2, 128, 25))
    # 2. Fat-finger order (QTY=250) rejected in 1 clock!
    st.send(OrderFrame(OP_NEW_ORDER, 1, 130, 250))
    # 3. Rogue algo flood: 3rd NewOrder passes, 4th & 5th throttled by hardware rate-limiter!
    st.send(OrderFrame(OP_NEW_ORDER, 3, 127, 15))
    st.send(OrderFrame(OP_NEW_ORDER, 1, 131, 10))
    st.send(OrderFrame(OP_NEW_ORDER, 1, 132, 10))
    # 4. Safety-critical Cancel bypasses active hardware rate-limiter!
    st.send(OrderFrame(OP_CANCEL, 1, 130, 10))
    # 5. Window refills after 420 us, then MID-PACKET KILL slices an order mid-TX!
    gate.advance_us(420.0)
    st.send(OrderFrame(OP_NEW_ORDER, 1, 135, 20), kill_mid_tx_clks=250, arm_trace=2)
    print(st.render(interactive=False))


def run_interactive(gate: ProteusHardwareGate) -> None:
    st = DemoState(gate)
    # Kick off with 1 clean order so the dashboard immediately shows live silicon stats!
    st.send(OrderFrame(OP_NEW_ORDER, 1, 130, 10), arm_trace=1)

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            sys.stdout.write(st.render(interactive=True) + "\n")
            sys.stdout.flush()

            timeout = 0.18 if st.rogue else 0.5
            rlist, _, _ = select.select([sys.stdin], [], [], timeout)
            if rlist:
                ch = os.read(fd, 1).decode("utf-8", errors="ignore").lower()
                if ch == "q":
                    break
                elif ch == "n":
                    sym = random.choice([1, 2, 3])
                    st.send(OrderFrame(OP_NEW_ORDER, sym, random.randint(124, 134), random.randint(5, 40)), arm_trace=1)
                elif ch == "x":
                    st.send(OrderFrame(OP_CANCEL, 1, 128, 10), arm_trace=1)
                elif ch == "h":
                    st.send(OrderFrame(OP_HEARTBEAT, 0, 0, st.seq & 0x7F))
                elif ch == "f":
                    st.send(OrderFrame(OP_NEW_ORDER, 1, 130, 250))
                elif ch == "r":
                    st.rogue = not st.rogue
                elif ch == "k":
                    st.toggle_kill()
                elif ch == "m":
                    if not st.armed:
                        st.toggle_kill()
                    gate.advance_us(420.0)
                    st.send(OrderFrame(OP_NEW_ORDER, 1, 135, 20), kill_mid_tx_clks=250, arm_trace=2)
                elif ch == "w":
                    gate.advance_us(420.0)
                    st.log.append(f"{CYAN}>>> Advanced silicon clock +420 us (rl_timer refilled tokens) <<<{RST}")
            elif st.rogue:
                st.send(OrderFrame(OP_NEW_ORDER, 1, random.randint(128, 140), 10))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Proteus RTL v2 x Qubx Hardware Risk Gate Demo")
    ap.add_argument("--showcase", action="store_true", help="Run scripted showcase non-interactively")
    ap.add_argument("--serial", default=None, help="Pico 2 USB-serial port (e.g. /dev/tty.usbmodem1101) for live Tang Nano 9K FPGA")
    args = ap.parse_args()
    with ProteusHardwareGate(serial_port=args.serial) as gate:
        if args.showcase or not sys.stdin.isatty():
            run_showcase(gate)
        else:
            run_interactive(gate)


if __name__ == "__main__":
    main()
