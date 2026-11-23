# AGENTS.md — Proteus (RTL v2)

## The Pitch

**Learn it, become it — a 60 MHz silicon protocol chameleon in 28.8% of 1 mm².**

Named after the Greek shapeshifting sea-god who only reveals the truth when held and questioned,
`proteus` (`tt_um_mikekovetsky_proteus`) is a Tiny Tapeout IHP 130 nm CMOS5L (`8×4` tile) entry for
Jane Street's [Protocol Emulator ASIC Competition](https://blog.janestreet.com/protocol-emulator-asic-competition/).
Instead of putting another small CPU or bit-banged PIO on silicon, `proteus` is a **single-cycle,
SRAM-backed state-table protocol engine** co-designed for two things:

1. **Deterministic wire-speed protocol emulation** — up to 4 time-sliced state machines sharing two hard
   `512×32` IHP SRAMs (`512 × 64-bit` entries), executing bit shifts, pin ops, timers, LFSR/CRC steps,
   16-rule masked TCAM byte classification, and multi-way branches in **one 16.6 ns clock** with zero
   branch-mispredict jitter.
2. **Silicon-accelerated reverse engineering ("Learn it, become it")** — attach an undocumented SPI or I2C
   black-box IC to the pins; the chip runs high-speed probe tables, the host infers the device's exact
   minimal automaton (including undocumented commands, hidden registers, hidden secondary I2C addresses,
   and multi-step unlock sequences), compiles a standalone **RTL v2 TCAM + hardware `nRESET`** table,
   loads it back into the chip, and turns the chip into a **drop-in, wire-speed hardware clone** serving
   SPI at **5–6 MHz** and I2C at **400 kHz**.

The entire RTL v2 chip — including both 512×32 SRAMs, the 16-rule masked TCAM, dual-edge 8.33 ns input
samplers, 4th-input muxing, and a 32-entry hardware pin-change trace buffer — closes full post-PnR
sign-off at **60 MHz** in **28.8% effective utilization** with **+3.55 ns slow-corner setup slack**,
**0 slew/cap/setup/hold/DRC/LVS/antenna violations**, a clean Tiny Tapeout KLayout precheck, and
**130/130 gate-level tests passing** on the routed IHP 130 nm netlist.

---

## Key Selling Points & Architecture Features

### 1. Hardware Architecture (RTL v2, 100% backward-compatible with v1 images)
- **Single-cycle SRAM state-table core (`src/proteus_core.v`, `src/proteus_table.v`):**
  512 × 64-bit table entries stored in two hard `RM_IHPSG13_1P_512x32_c2_bm_bist` macros. Each clock
  executes pin output ops (low/high/release/data/negated-data/toggle/open-drain), serial shift, 16-bit
  timer, 16-bit counter `N`, 4-bit counters `K`/`J`, 32-bit programmable LFSR/CRC, FIFO push/pop, call/return,
  and 2- or 4-way conditional next-state lookup in one cycle. Up to 4 independent machines time-slice the
  shared datapath (1 machine @ 60 MHz, 2 @ 30 MHz, 4 @ 15 MHz).
- **1-clock indexed dispatch & shared 16-rule masked Byte-Class TCAM (`gate == 3`):**
  Matches byte register `S` against 16 priority-encoded `(val, mask, class)` rules in parallel with the
  SRAM read, then jumps directly into a 4-, 8-, or 16-entry aligned table (`tcam4`, `tcam8`, `tcam16`) or
  slices `S` directly (`S[7:4]`, `S[3:0]`, `S[n+1:n]`) in **1 clock** — eliminating multi-clock compare
  chains and decision trees on command opcodes and bit-fields.
- **8.33 ns dual-edge input sampling (`in_b_half` / `P2.half` + `edge_b`):**
  A 3-stage dual-edge input synchronizer (`src/project.v`) captures `pins_half` on `negedge clk` (8.33 ns
  ahead of `posedge clk`), doubling effective edge resolution to **120 MHz** on a 60 MHz clock. Powers an
  on-chip 3-point deadband DPLL for 10BASE-T Ethernet receive.
- **4th per-machine input pin (`in_d`):**
  Reclaims condition 12 (`rx3`) as a 4th physical input pin when `in_d_en = 1`, enabling SPI targets
  (`SCK`, `MOSI`, `CS`) to monitor hardware `nRESET` on `P4` directly in silicon.
- **32-entry Port-3 hardware pin-change trace buffer (`src/proteus_trace.v`):**
  Passive on-chip logic analyzer active whenever `running == 1`. Records `{delta[7:0], pins[7:0]}` on any
  masked pin transition (with `0xFF` cycle-delta heartbeats, programmable pin mask, and arm-on-trigger)
  for exact cycle-accurate protocol observation through Host Port 3.
- **Host-read strobe guard (`src/project.v`):**
  Stray `rd_strobe` edges while `HAS_BYTE == 0` are ignored so host software can never desynchronize the
  nibble link.
- **All-async-reset flip-flops (R4):**
  Every one of the 2,105 synthesized flip-flops resets asynchronously via `rst_n` (verified by
  `test/synth/generic.ys`), with zero reliance on power-up values.

### 2. Flagship Novelty: "Learn it, become it" (`learn/`)
- **Active black-box protocol learning through the real chip pins:**
  The host drives the chip strictly over the Tiny Tapeout nibble link (`learn/lab/jail.py` enforces
  isolation — zero access to device internals, verified by `test_isolation.py`).
- **Discovers undocumented behavior:**
  Finds hidden SPI test modes, hidden key commands, hidden I2C registers, hidden secondary I2C addresses,
  and multi-step unlock sequences, and emits a readable datasheet-like account (`learn/host/account.py`)
  plus explicit soundness/warning guarantees.
- **Wire-speed drop-in cloning with RTL v2:**
  `learn/host/clone_spi.py` uses 8-bit priority hypercube compression to synthesize 1-clock TCAM dispatch
  tables plus a shared `wait_cs` subroutine monitoring `nRESET` on `P4`. The learned SPI Flash clone serves
  at **5 MHz and 6 MHz** (up from the v1 compare-tree bottleneck), honors hardware `nRESET` from every
  state, and passes **two-chip Icarus replacement** (`learn/lab/replace.py`: unmodified shipped controller
  talking to the real device vs. swapped-in cloned chip, plus negative controls).
- **MicroPython-compatible host (`learn/host/`):**
  Pure standard-subset Python with zero external imports, designed to run on the Tiny Tapeout demo board's
  RP2350 microcontroller.

### 3. Protocol Coverage (All 5 Prior Gaps Closed)
- **Core protocols:**
  - **UART:** 9600, 115200, 921600, and 3,000,000 baud (TX & RX), plus worked 8E1 example (`docs/examples/`).
  - **SPI:** Controller modes 0, 1, 2, 3; Target mode 0 (and learned 5–6 MHz v2 TCAM targets with `nRESET`).
  - **I2C:** 100 kHz & 400 kHz open-drain controller (write, repeated-START write-then-read, NACK/stuck-bus
    detection, and v2 **plain read** via `addr | 0x80`), plus learned 400 kHz open-drain targets with `nRESET`.
- **Stretch protocols (line coding, CRC, and clock recovery on chip):**
  - **10BASE-T Ethernet TX (`eth10_tx.fsm`):** On-chip Manchester encoding, CRC-32, and **16.0 ms idle Normal
    Link Pulses** (`gap_eth10_link_pulses` closed).
  - **10BASE-T Ethernet RX (`eth10_rx.fsm`):** On-chip Manchester decoding, CRC-32 check, and an **8.33 ns
    3-point deadband DPLL** passing IEEE 802.3 **±13.5 ns jitter + ±400 ppm drift** (`gap_eth10_rx_jitter`
    closed).
  - **USB 1.1 Low-Speed TX & RX (`usb_ls_tx.fsm`, `usb_ls_rx.fsm`):** On-chip NRZI coding, bit-stuffing /
    unstuffing, CRC-5/CRC-16, and **1-byte ACK/NAK/STALL handshakes** (via `S[1:0]` dispatch on TX and TCAM
    on RX).
- **Additional protocols:** 1-Wire (`onewire.fsm`), PS/2 RX (`ps2_rx.fsm`), WS2812B LED TX (`ws2812_tx.fsm`).

---

## Verified Evidence (Exact Measured Numbers)

All results below are measured on `rtl-v2` (commit `78be664`, merged to `main`):

| Verification Layer | Result | Source / Run |
|---|---|---|
| **IHP 130 nm GDS + Layout Report** | **PASS** — 28.8% util, 0 DRC, 0 LVS, 0 antenna, 0 slew/cap/setup/hold vios in all 3 corners; **+3.5512 ns** slow setup WS, **+0.0997 ns** fast hold WS at 60 MHz | GitHub run [45097155765](https://github.com/MikeKovetsky/proteus/actions/runs/45097155765) (`gds`) |
| **Tiny Tapeout KLayout Precheck** | **PASS** — `Precheck passed for tt_um_mikekovetsky_proteus.gds!` | GitHub run [45097155765](https://github.com/MikeKovetsky/proteus/actions/runs/45097155765) (`precheck`) |
| **Gate-Level Simulation (routed netlist)** | **130 / 130 PASS** (`FAIL=0, SKIP=0`) | GitHub run [45097155765](https://github.com/MikeKovetsky/proteus/actions/runs/45097155765) (`gl_test`) |
| **Independent 3rd-Party Protocol Oracles** | **6 / 6 PASS** against unmodified `cocotbext-uart` (0.1.4), `cocotbext-i2c` (0.1.2), `cocotbext-spi` (0.6.2) (UART 115.2k/921.6k, I2C 400k/100k/plain-read/NACK, SPI modes 0–3, 5 MHz v2 TCAM SPI target) | `test/oracle/` & GitHub run [45097155470](https://github.com/MikeKovetsky/proteus/actions/runs/45097155470) |
| **Learn-it, Become-it Suite (`test/learn`)** | **82 / 82 PASS** (Flash 5 MHz & 6 MHz TCAM + `P4` `nRESET`, 2-chip Icarus replacement, 12 negative controls, jail isolation, MicroPython syntax) | Local `LEARN_JOBS=2 make -C test/learn` |
| **Golden-Model Lockstep & Mutants** | **323 / 323 traces** (3,050,238 clocks) match; **21 / 21** RTL mutants killed | `make -C test mutants` & GitHub run [45097155470](https://github.com/MikeKovetsky/proteus/actions/runs/45097155470) |
| **Compiler, Bake-off & Flow Suites** | **68 / 68** compiler tests, **14 / 14** bake-off benches, **50 / 50** flow tests, **0** Verilator lint warnings | `test_compiler`, `bakeoff.run`, `test/flow`, `make -C test lint` |

### Honest Limits (Keep Claims Grounded)
- **Simulation & post-PnR sign-off only so far:** No program has been run on physical FPGA or fabricated
  silicon yet.
- **Max-fanout warnings remain:** 150 CTS/fanout warnings (`clkbuf_leaf_*` fanout 15–17 vs. limit 8) remain
  in OpenSTA; they do not fail timing, slew, cap, DRC, LVS, or precheck.
- **Submission items still open before 2027-01-18:** Finalize `WRITEUP.md`, update `docs/architecture.md` /
  `docs/isa.md` / `docs/programming.md` with RTL v2 features, and submit via the competition form once posted.

---

## Agent Operating Rules

1. **Read `CLAUDE.md` first** for the full requirements checklist, history, and tool setup.
2. **Never read or write `../protoemu-holdout/`**, never edit `judge/`, and never weaken frozen tests.
3. **Environment:** Source `~/oss-cad-suite/environment` before running `verilator`, `iverilog`, `yosys`,
   `sby`, `mcy`, or cocotb tests locally. Keep parallel heavy jobs bounded (`LEARN_JOBS=2`).
4. **Tests are gates:** Never commit with a lower passing test count, always report exact test counts in
   commit messages, and record AI contributions honestly in `notes/ai-usage.md`.
5. **Quick verification commands:**
   ```bash
   source ~/oss-cad-suite/environment
   make -C test lint
   make -C test/synth generic ihp timing
   python3 -m bakeoff.cand_table.test_compiler
   python3 -m bakeoff.run --candidate cand_table --strict
   python3 -m unittest discover -s test/flow
   make -C test mutants
   make -C test parallel SIM=verilator   # all 130 RTL pin tests in ~17s
   make -C test parallel                 # all 130 RTL pin tests on Icarus across 4 cores
   make -C test/oracle -B
   LEARN_JOBS=2 make -C test/learn
   ```
