# Proteus (`tt_um_mikekovetsky_proteus`) — Competition Write-Up

**Learn it, become it — a 60 MHz silicon protocol chameleon in 28.8% of 1 mm².**

`proteus` is an open-source, reprogrammable protocol-emulator ASIC for Jane Street's [Protocol Emulator ASIC Competition](https://blog.janestreet.com/protocol-emulator-asic-competition/), targeting Tiny Tapeout's IHP 130 nm CMOS5L (`8×4` tile, ~1.0 mm²) shuttle.

---

## 1. Architecture Choice & Bake-Off

Rather than guessing an architecture up front, we built **14 protocol benchmark scenarios** across 8 protocols (UART, SPI, I2C, WS2812B, 1-Wire, PS/2, USB 1.1 Low-Speed, 10BASE-T Ethernet) and raced **three candidate Python simulator architectures** (`bakeoff/`):

1. **Table-driven state machine (`cand_table` — selected):** A program is a 512 × 64-bit state table held in two hard `512×32` IHP SRAM macros. On every 60 MHz clock, one SRAM entry selects up to four next states via OR-based bit-splicing (`next = base | cA | cB << 1`), performs up to two pin operations, shifts serial data, steps a 32-bit programmable LFSR/CRC, updates timers/counters (`T`, `N`, `K`, `J`), and moves one byte on the internal bus. Up to **4 machines time-slice the shared datapath** (1 @ 60 MHz, 2 @ 30 MHz, 4 @ 15 MHz).
2. **PIO-style tiny ISA (`cand_pio`):** Familiar shift/delay loops, but multi-way protocol branches and CRC/line-coding cost extra cycles and code density.
3. **Event-driven trigger machine (`cand_event`):** Compact on simple edges, but harder to guarantee uniform cycle timing and clean physical timing closure.

`cand_table` won on **determinism (zero branch-mispredict jitter)**, **single-cycle multi-way dispatch**, and **predictable SRAM-to-SRAM timing closure at 60 MHz**.

### RTL v2 Extensions (Backward-Compatible)
After closing the initial v1 flow at 60 MHz, we added five compact hardware features (still fitting in **28.8% effective utilization**):
- **1-clock indexed dispatch & 16-rule masked Byte-Class TCAM (`gate == 3`):** Evaluates shift register `S` against 16 priority-encoded `(val, mask, class)` rules in parallel with the SRAM read (`tcam4`, `tcam8`, `tcam16`, `S[7:4]`, `S[3:0]`, `S[hi:lo]`). Eliminates multi-clock compare trees on opcodes and packet fields.
- **8.33 ns dual-edge input sampling (`P2.half` / `edge_b`):** A 3-stage `negedge clk` synchroniser in `src/project.v` doubles edge resolution to **120 MHz** on a 60 MHz clock, powering a 3-point deadband DPLL for 10BASE-T Ethernet receive.
- **4th physical input pin (`in_d`):** Reclaims condition 12 (`rx3`) so SPI/I2C targets and inline gates monitor hardware `nRESET` / kill-switch on `P4` directly in silicon.
- **32-entry Port-3 hardware pin-change trace buffer (`src/proteus_trace.v`):** Built-in cycle-accurate logic analyzer snooping `P0..P7` with programmable watch mask, arm-on-trigger, and `0xFF` delta heartbeats.
- **Host-read strobe guard (`src/project.v`):** Ignores stray `rd_strobe` edges when `HAS_BYTE == 0`.

---

## 2. Flagship Novelty: *"Learn It, Become It"* (`learn/`)

Attaching an undocumented SPI or I2C black-box IC to Proteus's pins turns the chip into a **silicon-accelerated reverse-engineering and cloning engine**:

1. **Isolated probing through real pins:** A MicroPython-compatible host (`learn/host/`, zero third-party imports, designed for Tiny Tapeout's RP2350) drives Proteus strictly over the Tiny Tapeout nibble link (`learn/lab/jail.py` enforces zero visibility into target internals, verified by `test_isolation.py`).
2. **Active automaton discovery:** Proteus runs high-speed probe tables; the host infers the device's minimal state machine — discovering undocumented SPI test modes, hidden key commands, hidden I2C registers, secondary I2C addresses, and multi-step unlock sequences — and prints a datasheet-like account (`learn/host/account.py`) with explicit soundness bounds.
3. **Wire-speed drop-in cloning:** `learn/host/clone_spi.py` compresses 8-bit opcode tables into 1-clock RTL v2 TCAM dispatch plus a shared `wait_cs` subroutine monitoring hardware `nRESET` on `P4`. The resulting standalone image runs on Proteus at **5 MHz and 6 MHz SPI** (and **400 kHz open-drain I2C**), passing **two-chip Icarus replacement** (`learn/lab/replace.py`: unmodified controller talking to real device vs. swapped-in Proteus clone, plus 12 negative controls).

---

## 3. Domain Showcase: Proteus × Qubx 60 MHz Inline Hardware Risk Gate

To demonstrate multi-machine concurrency, 1-clock TCAM dispatch, `P4` hardware interrupts, and Port-3 observability on a real-world workload, [`examples/qubx_risk_gate/`](examples/qubx_risk_gate/) compiles two concurrent state machines (**127 / 512 SRAM entries, 24.8%, 1,061-byte image**) onto unmodified Proteus RTL v2, paired with a pluggable pre-trade `OrderGate` interface for [xLydianSoftware/Qubx](https://github.com/xLydianSoftware/Qubx):

- **Machine 0 (`risk_gate`):** Parses 4-byte binary Mini-OUCH frames on `P0` (`3 Mbaud` @ 60 MHz), classifies opcodes in **1 clock** via TCAM (`'O'` NewOrder, `'X'` Cancel, `'H'` Heartbeat, or immediate `BAD_OPCODE` drop), enforces a **1-clock fat-finger guard** (`QTY >= 128` via `S[7:4]`), enforces a hardware token-bucket rate limiter on NewOrders while **always letting safety-critical Cancels bypass**, and monitors physical `KILL_N` (`P4`) pre-frame and on every transmitted bit.
- **Machine 1 (`rl_timer`):** Independent 16-bit hardware window clock pulsing inter-machine semaphore `P5` every `400 µs`.
- **Measured on unmodified Proteus RTL v2 (Verilator, 60 MHz):**
  - `NewOrder` wire-to-wire turnaround: **150.0 ns (9 clocks)**, **0.0 ns jitter** (`p50 == p99 == max`)
  - `Cancel` bypass turnaround: **83.3 ns (5 clocks)**, **0.0 ns jitter**
  - Mid-packet physical `P4` kill-switch cutoff: TX severed and `ALERT_KILL` asserted in **6 clocks (100.0 ns)** including 2-flop input synchroniser, captured live in the **Port-3 hardware logic analyzer**.

---

## 4. Verification Methodology & Exact Measured Evidence

All figures below are measured on `proteus` RTL v2 (`src/` frozen and unmodified after sign-off):

| Verification Layer | Result | Scope & Evidence |
|---|---|---|
| **IHP 130 nm GDS Sign-off** | **PASS @ 60 MHz** | 28.8% effective util, **+3.5512 ns** slow setup WS, **+0.0997 ns** fast hold WS, **0** DRC / LVS / antenna / slew / cap / setup / hold violations in all 3 corners ([run 45097155765](https://github.com/MikeKovetsky/proteus/actions/runs/45097155765)) |
| **Tiny Tapeout KLayout Precheck** | **PASS** | `Precheck passed for tt_um_mikekovetsky_proteus.gds!` |
| **Routed Gate-Level Simulation** | **130 / 130 PASS** | `FAIL=0, SKIP=0` on extracted IHP 130 nm CMOS5L netlist (`gl_test`) |
| **Independent 3rd-Party Oracles** | **6 / 6 PASS** | Unmodified `cocotbext-uart` 0.1.4, `cocotbext-i2c` 0.1.2, `cocotbext-spi` 0.6.2 (`test/oracle/`, [run 45097155470](https://github.com/MikeKovetsky/proteus/actions/runs/45097155470)) |
| **Golden-Model Lockstep & Mutants** | **323 / 323 traces, 21 / 21 mutants** | 3,050,238 clocks cycle-identical between Python model and Verilator RTL; all 21 planted RTL mutants killed (`make -C test mutants`) |
| **Reset Proof (R4)** | **2,105 / 2,105 async-reset FFs** | Yosys generic synthesis check verifies every synthesized flop resets asynchronously via `rst_n` (`test/synth/generic.ys`) |
| **Learn-It, Become-It Suite** | **82 / 82 PASS** | 5 MHz & 6 MHz SPI Flash TCAM + `P4` `nRESET`, 400 kHz I2C clones, 2-chip Icarus replacement, 12 negative controls, jail isolation, MicroPython syntax (`make -C test/learn`) |
| **Compiler, Bake-off & Flow Suites** | **68 / 68, 14 / 14, 50 / 50** | Compiler unit + fuzz checks, all 14 bake-off benches, flow/config checks, 0 Verilator lint warnings |
| **Tang Nano 9K FPGA Port** | **PASS @ 27.00 MHz** | Open-source Gowin `yosys` + `nextpnr-himbaechel` + `gowin_pack`: `Fmax = 28.75 MHz`, 3,722/8,640 LUT4 (43%), 2,270/6,480 DFF (35%), 8/26 BSRAM (30%), 0 errors (`fpga/tang_nano_9k/`) |
| **Qubx Risk Gate & Pico 2 Host** | **4 / 4 + 2 / 2 PASS** | `make -C examples/qubx_risk_gate test` + `python3 fpga/tang_nano_9k/test_pico2_host.py` |

### Protocol Coverage (All 5 v1 Gaps Closed)
- **UART:** 9600, 115200, 921600, and 3,000,000 baud (TX & RX), plus worked 8E1 example (`docs/examples/`).
- **SPI:** Controller modes 0, 1, 2, 3; Target mode 0 (plus learned 5–6 MHz v2 TCAM targets with `nRESET`).
- **I2C:** 100 kHz & 400 kHz open-drain controller (write, repeated-START write-then-read, plain read via `addr | 0x80`, NACK/stuck-bus detection), plus learned 400 kHz open-drain targets.
- **10BASE-T Ethernet:** On-chip Manchester TX/RX, CRC-32, 16.0 ms Normal Link Pulses (`gap_eth10_link_pulses` closed), and 8.33 ns 3-point deadband DPLL passing IEEE 802.3 ±13.5 ns jitter + ±400 ppm drift (`gap_eth10_rx_jitter` closed).
- **USB 1.1 Low-Speed:** On-chip NRZI coding, bit stuffing/unstuffing, CRC-5/CRC-16, and 1-byte ACK/NAK/STALL/NYET handshakes.
- **Additional:** 1-Wire, PS/2 RX, WS2812B LED TX, and 4-byte binary Mini-OUCH risk gate.

---

## 5. Honest Limits & Remaining Work Before Final Tape-Out

1. **Physical hardware bring-up pending shipping:** The Tang Nano 9K FPGA bitstream (`fpga/tang_nano_9k/build/proteus_tang9k.fs`) and Pico 2 MicroPython host firmware (`fpga/tang_nano_9k/pico2_host.py`) are built and verified in PnR/unit tests, while physical boards (Tang Nano 9K, Pico 2, W25Q32, LM75A) are currently in transit for live breadboard verification and video capture.
2. **OpenSTA max-fanout warnings:** 150 clock-tree leaf fanout warnings (`clkbuf_leaf_*` fanout 15–17 vs. limit 8) remain in OpenSTA; they do not cause setup, hold, slew, capacitance, DRC, LVS, or precheck violations.
3. **Blind "stranger test" not yet run:** Held-out protocols (CAN, JTAG, SWD) exist outside the repo and have not yet been executed against `proteus`.
4. **Gate-level sim uses zero-delay functional netlist:** Post-PnR timing closure is verified by OpenSTA across all three corners on extracted parasitics, while `gl_test` runs functional simulation on the routed netlist.

---

## 6. How AI Tools Were Used

This project was developed by Mike Kovetsky (co-founder of [xLydianSoftware](https://github.com/xLydianSoftware)) with extensive AI assistance (Claude Code / Opus / Fable and Google Antigravity / Gemini), using **tests and adversarial reviews as hard gates**. A complete dated engineering log is maintained in [notes/ai-usage.md](notes/ai-usage.md).

In summary:
- **Human decisions:** Architecture selection from the 3-candidate bake-off, RTL v2 scope and novelty direction (*"Learn it, become it"*), project naming (`proteus`), Tang Nano 9K FPGA hardware selection, and the xLydian `Qubx` pre-trade hardware risk-gate integration.
- **AI execution & adversarial review:** AI agents drafted the bake-off harness and candidates, RTL, compiler, `learn/` engine, independent third-party `cocotbext` oracle suite, LibreLane floorplan/slew fixes, FPGA port, and documentation. Separate fresh reviewer agents (given diffs and requirements without builder rationales) repeatedly caught lenient tests, compiler bugs, and electrical/flow issues — and every claim in this write-up is backed by reproducible commands in CI and locally.
