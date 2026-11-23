# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Qubx-compatible Pre-Trade OrderGate backed by Proteus RTL v2 silicon (Verilator or FPGA).

Architecture:
  Strategy (Qubx) -> OrderGate (ProteusHardwareGate / SoftwareOrderGate) -> Exchange / OME

Every order is serialized into a 4-byte binary Mini-OUCH frame [OPCODE, SYM_ID, PRICE, QTY]
and transmitted bit-by-bit as real 3.000 Mbaud UART electrical signals onto Proteus pin P0.
Proteus executes its 2-machine RTL v2 state table (1-clock TCAM opcode dispatch, 1-clock
fat-finger guard, hardware token-bucket rate limiter, and physical P4 kill-switch) with zero
host CPU involvement, forwarding approved orders directly onto P1 with 0 ns jitter.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bakeoff.cand_table.compiler import compile_file  # noqa: E402

HERE = Path(__file__).resolve().parent
FSM_PATH = HERE / "risk_gate.fsm"
SIM_BIN = HERE / "build" / "sim_risk_gate"

CLK_HZ = 60_000_000
NS_PER_CLK = 1e9 / CLK_HZ  # 16.6667 ns

OP_NEW_ORDER = 0x4F  # 'O'
OP_CANCEL = 0x58     # 'X'
OP_HEARTBEAT = 0x48  # 'H'

AUDIT_NAMES = {
    0xA0: "PASS_NEW_ORDER",
    0xA1: "PASS_CANCEL",
    0xA2: "PASS_HEARTBEAT",
    0xE1: "DROP_RATE_LIMIT",
    0xE2: "DROP_KILL_SWITCH",
    0xE3: "DROP_BAD_OPCODE",
    0xE4: "DROP_FAT_FINGER",
}

SYMBOLS = {
    1: ("BTCUSDT", 95000.0, 10.0),
    2: ("ETHUSDT", 3200.0, 1.0),
    3: ("SOLUSDT", 210.0, 0.25),
}


@dataclass
class OrderFrame:
    opcode: int
    sym_id: int
    price_tick: int
    qty: int

    def to_bytes(self) -> bytes:
        return bytes([self.opcode & 0xFF, self.sym_id & 0xFF, self.price_tick & 0xFF, self.qty & 0xFF])

    @classmethod
    def from_bytes(cls, data: bytes) -> Optional["OrderFrame"]:
        if len(data) != 4:
            return None
        return cls(data[0], data[1], data[2], data[3])

    def label(self) -> str:
        op_s = {OP_NEW_ORDER: "NEW", OP_CANCEL: "CXL", OP_HEARTBEAT: "HB "}.get(self.opcode, f"0x{self.opcode:02X}")
        sym = SYMBOLS.get(self.sym_id, (f"SYM{self.sym_id}", 100.0, 1.0))[0]
        return f"{op_s} {sym:7s} px={self.price_tick:3d} qty={self.qty:3d}"


@dataclass
class GateResult:
    sent: OrderFrame
    forwarded: Optional[OrderFrame]
    raw_tx: bytes
    audits: List[int]
    lat_clks: int
    kill_resp_clks: int
    tx_abort: bool
    alert_rl: bool
    alert_kill: bool
    overflow: bool
    trace: List[Tuple[int, int]] = field(default_factory=list)  # list of (pins, delta_clks)

    @property
    def passed(self) -> bool:
        return self.forwarded is not None and self.forwarded == self.sent

    @property
    def lat_ns(self) -> Optional[float]:
        return None if self.lat_clks < 0 else self.lat_clks * NS_PER_CLK

    @property
    def kill_resp_ns(self) -> Optional[float]:
        return None if self.kill_resp_clks < 0 else self.kill_resp_clks * NS_PER_CLK

    @property
    def verdict(self) -> str:
        if not self.audits:
            return "NO_AUDIT"
        return "+".join(AUDIT_NAMES.get(a, f"0x{a:02X}") for a in self.audits)


class OrderGate:
    """Pluggable Qubx pre-trade OrderGate interface.

    In live production, Qubx swaps in a low-latency Rust/C sidecar implementing this same
    submit() / set_kill_switch() contract. For the Jane Street ASIC competition,
    ProteusHardwareGate runs the exact check in 60 MHz Proteus silicon over physical pins.
    """

    def submit(self, order: OrderFrame, *, kill_mid_tx_clks: int = -1, arm_trace: int = 0) -> GateResult:
        raise NotImplementedError

    def set_kill_switch(self, armed: bool) -> dict:
        raise NotImplementedError

    def advance_us(self, us: float) -> dict:
        raise NotImplementedError


class ProteusHardwareGate(OrderGate):
    """Hardware-in-the-loop OrderGate driving unmodified Proteus RTL v2 via Verilator or Pico 2 USB-serial."""

    def __init__(
        self,
        fsm_path: Path = FSM_PATH,
        sim_bin: Path = SIM_BIN,
        serial_port: Optional[str] = None,
        serial_baud: int = 115200,
    ):
        self.fsm_path = fsm_path
        self.sim_bin = sim_bin
        self.serial_port = serial_port
        self.compiled = compile_file(fsm_path)
        self._proc = None
        self._ser = None

        if serial_port:
            try:
                import serial  # type: ignore
            except ImportError as exc:
                raise RuntimeError("pyserial required for --serial mode: `pip install pyserial`") from exc
            self._ser = serial.Serial(serial_port, serial_baud, timeout=2.0)
        else:
            if not sim_bin.exists():
                raise FileNotFoundError(f"Verilator simulator not built: {sim_bin}. Run `make -C {HERE} sim`.")
            self._proc = subprocess.Popen(
                [str(sim_bin)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        resp = self._cmd(f"LOAD {self.compiled.image.hex()}")
        if not resp.get("ok") or not resp.get("running"):
            raise RuntimeError(f"Failed to load Proteus risk_gate image: {resp}")

    def _cmd(self, line: str) -> dict:
        if self._ser is not None:
            self._ser.write((line + "\n").encode("ascii"))
            self._ser.flush()
            out = self._ser.readline().decode("utf-8", errors="replace")
            if not out:
                raise RuntimeError("Pico 2 serial bridge timed out")
            return json.loads(out)
        assert self._proc and self._proc.stdin and self._proc.stdout
        self._proc.stdin.write(line + "\n")
        self._proc.stdin.flush()
        out = self._proc.stdout.readline()
        if not out:
            raise RuntimeError("Proteus simulator exited unexpectedly")
        return json.loads(out)

    def submit(self, order: OrderFrame, *, kill_mid_tx_clks: int = -1, arm_trace: int = 0) -> GateResult:
        resp = self._cmd(f"SEND_FRAME {order.to_bytes().hex()} {kill_mid_tx_clks} {arm_trace}")
        raw_tx = bytes.fromhex(resp["tx_hex"])
        audits = list(bytes.fromhex(resp["audits_hex"]))
        return GateResult(
            sent=order,
            forwarded=OrderFrame.from_bytes(raw_tx),
            raw_tx=raw_tx,
            audits=audits,
            lat_clks=resp["lat_clks"],
            kill_resp_clks=resp["kill_resp_clks"],
            tx_abort=resp["tx_abort"],
            alert_rl=bool(resp["alert_rl"]),
            alert_kill=bool(resp["alert_kill"]),
            overflow=bool(resp["overflow"]),
            trace=[(int(p), int(d)) for p, d in resp.get("trace", [])],
        )

    def set_kill_switch(self, armed: bool) -> dict:
        return self._cmd(f"SET_KILL {1 if armed else 0}")

    def advance_us(self, us: float) -> dict:
        clks = max(1, int(round(us * CLK_HZ / 1e6)))
        return self._cmd(f"ADVANCE {clks}")

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        if self._proc:
            if self._proc.poll() is None:
                try:
                    self._cmd("QUIT")
                except Exception:
                    pass
                self._proc.terminate()
                self._proc.wait(timeout=3)
            if self._proc.stdin:
                self._proc.stdin.close()
            if self._proc.stdout:
                self._proc.stdout.close()
            self._proc = None

    def __enter__(self) -> "ProteusHardwareGate":
        return self

    def __exit__(self, *args) -> None:
        self.close()


class SimulatedOME:
    """Minimal Qubx-style Order Matching Engine receiving orders off Proteus TX wire P1."""

    def __init__(self):
        self.filled_orders: int = 0
        self.cancelled_orders: int = 0
        self.heartbeats: int = 0
        self.position_qty: Dict[str, int] = {v[0]: 0 for v in SYMBOLS.values()}
        self.notional_usd: float = 0.0

    def on_wire_frame(self, frame: OrderFrame) -> str:
        sym, base_px, tick_sz = SYMBOLS.get(frame.sym_id, (f"SYM{frame.sym_id}", 100.0, 1.0))
        px = base_px + (frame.price_tick - 128) * tick_sz
        if frame.opcode == OP_NEW_ORDER:
            self.filled_orders += 1
            self.position_qty[sym] = self.position_qty.get(sym, 0) + frame.qty
            self.notional_usd += px * frame.qty
            return f"FILL {sym} +{frame.qty} @ ${px:,.2f}"
        if frame.opcode == OP_CANCEL:
            self.cancelled_orders += 1
            return f"CXL_ACK {sym} qty={frame.qty}"
        if frame.opcode == OP_HEARTBEAT:
            self.heartbeats += 1
            return f"HB_ACK seq={frame.qty}"
        return f"UNKNOWN_OP 0x{frame.opcode:02X}"


def render_port3_waveform(trace: List[Tuple[int, int]], max_cols: int = 60) -> List[str]:
    """Render Port-3 hardware pin-change trace events as crisp ASCII logic-analyzer traces."""
    if not trace:
        return ["  (no Port-3 trace events captured)"]
    # Expand events into proportional visual columns
    names = ["P0 RX_IN  ", "P1 TX_OUT ", "P2 RL_ALRT", "P3 KILL_AL", "P4 KILL_N "]
    # Sample timeline proportionally so bursts and the exact kill edge are crystal clear
    total_clks = sum(max(1, d) for _, d in trace)
    samples: List[int] = []
    for pins, delta in trace:
        reps = max(1, int(round(max(1, delta) * max_cols / max(1, total_clks))))
        samples.extend([pins] * reps)
    samples = samples[:max_cols]
    lines = [f"  Port-3 Hardware Logic Analyzer ({len(trace)} HW events, {total_clks} clks = {total_clks * NS_PER_CLK:.0f} ns @ 60 MHz)"]
    for bit, name in enumerate(names):
        row = []
        prev = None
        for p in samples:
            v = (p >> bit) & 1
            if prev is not None and v != prev:
                row.append("╱" if v else "╲")
            else:
                row.append("‾" if v else "_")
            prev = v
        lines.append(f"  {name} |{''.join(row)}|")
    return lines
