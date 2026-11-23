# Proteus × Qubx — Inline Pre-Trade Hardware Risk Gate & 16.6 ns Kill-Switch

![Proteus x Qubx Hardware Risk Gate Interactive TUI](../../docs/img/qubx_risk_gate_demo.jpg)

A domain-native showcase demonstrating **unmodified Proteus RTL v2 silicon** operating as a deterministic, zero-jitter pre-trade hardware risk gate sitting inline between **[Qubx](https://github.com/xLydianSoftware/Qubx)** and an Order Matching Engine (`OME`).

## Quick Start (Judge Mode — No Hardware Required)

```bash
source ~/oss-cad-suite/environment
make -C examples/qubx_risk_gate test    # builds Verilator sim (~2s) and runs all 4 hardware tests (<0.1s)
make -C examples/qubx_risk_gate demo    # launches interactive TUI (press [N], [X], [F], [R], [K], [M])
```

Non-interactive scripted showcase:
```bash
python3 examples/qubx_risk_gate/demo_tui.py --showcase
```

## What Runs on Unmodified Proteus Silicon (`risk_gate.fsm`)

Compiled into **127 / 512 SRAM table entries (24.8%, 1,061-byte image, < 1 ms hot-load)** across **two concurrent state machines** (30 MHz/turn each @ 60 MHz ASIC clock):

| Hardware Feature | Role in `risk_gate.fsm` |
|---|---|
| **1-Clock Masked TCAM (`tcam4`)** | Classifies incoming 4-byte binary Mini-OUCH opcodes in 1 clock: `'O'` (`NewOrder`), `'X'` (`Cancel`), `'H'` (`Heartbeat`), or immediate `BAD_OPCODE` drop. |
| **1-Clock Slice Dispatch (`S[7:4]`)** | Fat-finger quantity guard (`QTY >= 128` rejected in 1 clock without consuming a rate-limit token). |
| **2 Concurrent Machines + Semaphore `P5`** | Machine 1 (`rl_timer`) runs an independent 16-bit hardware window clock (`400 µs`) and pulses semaphore `P5`; Machine 0 (`risk_gate`) enforces a `MAX_BURST=3` token-bucket rate limiter on `NewOrder`s while **always allowing safety-critical `Cancel` and `Heartbeat` frames through**. |
| **Physical Kill-Switch (`P4`)** | Monitored pre-frame **and on every transmitted bit**: yanking `P4` low (red jumper wire or Tang Nano 9K button S2) aborts in-flight TX within clocks and raises `ALERT_KILL` (`P3`). |
| **Port-3 Hardware Trace Buffer** | Passively timestamps `P0..P4` at 60 MHz (16.67 ns/clock) to verify **0 ns wire-to-wire jitter** and capture the exact mid-packet kill-switch edge. |

## Pluggable Qubx `OrderGate` Architecture (`qubx_gate.py`)

`OrderGate` defines a clean pre-trade boundary (`submit()`, `set_kill_switch()`) between a Qubx strategy and broker connector:
- **Contest / Hardware-in-the-Loop:** `ProteusHardwareGate` drives real binary 3 Mbaud UART electrical signals through unmodified Proteus RTL v2 (Verilator today; Tang Nano 9K + Pico 2 over physical wires using [fpga/tang_nano_9k/WIRING.md](../../fpga/tang_nano_9k/WIRING.md)).
- **Live Fund Production:** Swap in `SoftwareOrderGate` (or a Rust/C sidecar) implementing the exact same `IOrderGate` interface.

---

## Running Inside [xLydianSoftware/Qubx](https://github.com/xLydianSoftware/Qubx) ([PR #465](https://github.com/xLydianSoftware/Qubx/pull/465))

### 1. Verify Live Qubx `TradingManager` → 60 MHz Proteus Verilog

Build the Verilator simulator once in `proteus`, then run Qubx's order-gate test suite:

```bash
# In proteus:
source ~/oss-cad-suite/environment
make -C examples/qubx_risk_gate sim

# In Qubx (on branch feat/pre-trade-order-gate / PR #465):
PROTEUS_ROOT=~/repos/mike/protoemu uv run pytest tests/qubx/core/test_order_gate.py -v
```

This runs Qubx's real `TradingManager.trade()` and `cancel_order()` directly through unmodified 60 MHz Proteus Verilog, verifying rate-limit throttling, zero phantom order state on rejection, and Cancel bypass.

### 2. Wire Into Any Qubx Strategy (`on_init`)

#### Option A — Backed by Proteus Silicon (Verilator or Live Tang Nano 9K + Pico 2)

```python
from pathlib import Path
from qubx.core.interfaces import IStrategyInitializer
from qubx.core.order_gate import ProteusOrderGate

def on_init(self, initializer: IStrategyInitializer) -> None:
    ...
    # Verilator simulation today:
    gate = ProteusOrderGate(
        image_hex=risk_gate_image_hex,
        sim_bin=Path("~/repos/mike/protoemu/examples/qubx_risk_gate/build/sim_risk_gate").expanduser(),
    )

    # Or live Tang Nano 9K + Pico 2 breadboard over USB-serial:
    # gate = ProteusOrderGate(image_hex=risk_gate_image_hex, serial_port="/dev/tty.usbmodem1101")

    initializer.set_order_gate(gate)
```

Run your strategy normally (`uv run qubx simulate config.yml` or `uv run qubx run config.yml --paper`). Every `ctx.trade()` and `ctx.cancel_order()` is checked by Proteus **before** hitting `AccountManager` or the exchange connector; rejected orders raise `OrderGateRejected` with `.code` (`RATE_LIMIT`, `FAT_FINGER`, `KILL_SWITCH`) and `.decision.latency_ns`.

#### Option B — Live Production Trading (`SoftwareOrderGate`)

Swap one line to guard live capital using the exact same `IOrderGate` interface:

```python
from qubx.core.order_gate import SoftwareOrderGate

def on_init(self, initializer: IStrategyInitializer) -> None:
    ...
    initializer.set_order_gate(
        SoftwareOrderGate(
            max_orders=5,
            window_seconds=1.0,
            max_order_qty=10.0,
            max_order_notional=100_000.0,
        )
    )
```

At runtime from any callback or watchdog, `ctx.get_order_gate().set_kill_switch(False)` trips the kill-switch immediately while still allowing safety-critical cancels to pass.
