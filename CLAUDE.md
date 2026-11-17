# protoemu

An entry for Jane Street's
[protocol-emulator ASIC competition](https://blog.janestreet.com/protocol-emulator-asic-competition/): a small
programmable chip that speaks UART, SPI and I2C, and protocols it was not designed for. An AI agent builds it in
a scored loop, against a judge it cannot edit, and the chip goes out on Tiny Tapeout's IHP shuttle.

Status, 2026-11-16: the repo holds this plan only. Nothing is built.

## The competition, from the post (read 2026-11-16)

### Dates
1. Submissions close on 2027-01-18.
2. Selections are announced in early February 2027.
3. About four designs are taped out on the March 2027 IHP CMOS5L shuttle. Winners get their chip on a Tiny
   Tapeout demo board.

### Constraints
1. The process is IHP 130nm CMOS5L through Tiny Tapeout, starting from the
   [IHP Verilog template](https://github.com/TinyTapeout/ttihp-verilog-template).
2. The area is 8x4 tiles: 32 tiles, about 1.0 mm² nominal, each tile about 202 µm × 155 µm. It is set in
   `info.yaml`, and tile sizes are final. The budget is roughly 1K logic cells per tile.
3. SRAM macros are allowed, several if they fit and pass precheck. Synthesize early and leave room for clock-tree
   buffers and routing.
4. There are 24 digital pins (8 inputs, 8 outputs, 8 bidirectional), plus clock and `rst_n`. There are no analog
   pads.
5. Tiny Tapeout estimates I/O at about 10 ns round trip and about 66 MHz maximum output. These are estimates, not
   measurements.
6. There is no target clock. The design must close timing after place-and-route at the speed its protocols need.
   The demo board's RP2350 makes the clock, and Tiny Tapeout has confirmed 60 MHz works.
7. Flops do not power up at 0, so everything must reach a known state through `rst_n`.
8. The chip loses state when it is deselected, so the program must be reloaded on reselection.
9. GitHub Actions has a 6-hour limit. A custom GDS flow is allowed only above it, and it must still pass precheck.

### Protocols
1. The core protocols are UART, SPI (modes 0 to 3 suggested) and I2C (100 and 400 kHz suggested).
2. The stretch protocols are low-speed USB and 10 Mbit Ethernet. For these, the more of the line coding, CRC and
   clock recovery done on chip, the better.
3. Other suggestions are JTAG, SWD, PS/2 and CAN.
4. Concurrent protocols and both controller and target roles are a plus, not a requirement.
5. The post names the RP2040 PIO and TI Sitara PRU as inspiration.

### Deliverables
1. A public repository with RTL, tests and a passing GDS and precheck.
2. Architecture and instruction set documentation.
3. An assembler or toolchain (or a bitstream flow) with example programs.
4. A verification write-up.
5. Optionally, a video or demo.
6. Program loading is part of the design. The demo board's microcontroller is the natural host.

### Judging
1. Flexibility leads: can the chip run protocols it was not designed for?
2. The judges also weigh which protocols are shown, the architecture's unique abilities, and the design and
   verification approach.
3. The write-up is weighted heavily, including how readable it is and how clearly new techniques and flows are
   explained.
4. Fewer protocols with a strong architecture and verification can still compete.
5. AI use is allowed. The write-up must describe how AI tools were used.
6. Gate-level simulation is strongly recommended. Formal methods, constrained random tests and AI-assisted
   verification are named as interesting.

## Requirements checklist

Every line comes from the post. The builder and the reviewer each mark it MET or NOT MET with evidence (a file, a
test, a CI run). Nothing is submitted with a NOT MET line unless Mike has waived it in writing.

| # | Requirement | Status | Evidence |
|---|---|---|---|
| R1 | Built from the IHP Verilog template, 8x4 tiles set in `info.yaml` | NOT MET | |
| R2 | Reprogrammable after fabrication, within its timing and I/O limits | NOT MET | |
| R3 | UART, SPI and I2C demonstrated by programs | NOT MET | |
| R4 | Known state from `rst_n`, with no reliance on power-up values | NOT MET | |
| R5 | Program reload after deselect works | NOT MET | |
| R6 | Timing closes after place-and-route at the chosen clock (60 MHz target) | NOT MET | |
| R7 | GDS builds and precheck passes in Tiny Tapeout's GitHub Actions | NOT MET | |
| R8 | RTL and tests in the public repo | NOT MET | |
| R9 | Architecture and instruction set documentation | NOT MET | |
| R10 | Assembler or toolchain with example programs | NOT MET | |
| R11 | Verification write-up, including how AI was used | NOT MET | |
| R12 | Gate-level simulation passes | NOT MET | |
| R13 | Submitted before 2027-01-18 | NOT MET | |

## Plan: an agent loop with a judge it cannot edit

### Layout (target)

```
/                   Tiny Tapeout IHP template: src/, test/, docs/, info.yaml, .github/workflows/
asm/                assembler and example programs
judge/              the scorer; the worker never writes here
loop/               the driver script and the ledger of rounds
notes/lessons.md    what the loop has learned; the worker reads it every round
```

Held-out protocol tests live outside the repo, in `../protoemu-holdout/`. They are never committed, and the
worker can never read them.

### The judge (`judge/score.py`)

One command prints one JSON score.

1. **Gates.** A round that fails any gate scores zero:
   1. Verilator lint is clean.
   2. All core protocol tests pass (cocotb, against Python reference models of UART, SPI and I2C).
   3. The Yosys cell count fits the 32-tile budget, with headroom for routing.
   4. The mutation check passes: the judge plants known bugs in the RTL, and the tests must catch each one. Tests
      that pass on a broken design are worthless.
2. **Score.** It counts how many held-out protocols run as programs only (flexibility, the main judging
   criterion), the protocol modes and speeds covered, the area left over, gate-level simulation passing, and the
   sby properties proved. The weights start simple and get tuned once real runs exist.

### The worker loop (`loop/run.sh`)

1. Run the judge, then append the score and its failures to the ledger.
2. Run `claude -p` with a fixed prompt. It reads the last score, the failures and `notes/lessons.md`, makes one
   change and runs the judge.
3. Keep the commit if the score rose. Revert it if not. Log the round either way.
4. Stop at a fixed round cap. An unattended loop can burn a lot of tokens, so it runs on the Claude subscription
   or with a hard cap.

### Self-improvement

Every N rounds, a separate agent reads the ledger and rewrites `notes/lessons.md`: what kept failing, what
worked, and what to try next. A held-out task checks that the new lessons raise the score instead of just
changing it.

### The slow check

Once a night, push to GitHub so Tiny Tapeout's Actions build the real layout and run precheck. This takes hours,
so it stays out of the inner loop.

### Guards against a loop that games its score

1. The worker cannot write to `judge/`, the reference models or the holdout directory. This is enforced with
   deny rules in `.claude/settings.json`, not with a prompt.
2. The mutation check runs every round, so weakening the tests cannot raise the score.
3. A human checks the tests themselves. In Jane Street's August ASIC puzzle, one solver's simulator matched the
   sample waveform while it silently turned off a check. Matching a sample is not proof.

### What stays human

1. **The architecture and instruction set.** This is the main judging criterion, and the loop optimizes inside a
   design, not across designs.
2. **What counts as correct.** That means the reference models and the properties.
3. **The write-up.** The agent drafts it, and a person decides what it claims.

### Order of work

1. Install the OSS CAD Suite, bring in the template, set `info.yaml` to 8x4, and get CI green on the empty design.
2. Write judge v0: lint, reference models for UART, SPI and I2C, the cell count and the mutation check.
3. Sketch the architecture and instruction set by hand, then write the assembler.
4. Run the loop on the core protocols.
5. Add held-out protocols, gate-level simulation and sby proofs to the judge.
6. Turn on the nightly GDS and precheck run.
7. Attempt the stretch protocols: low-speed USB, then 10 Mbit Ethernet.
8. Write the docs, the verification write-up and the AI-usage section, then submit before 2027-01-18.

## The pieces

### Competition and flow
1. [The competition post](https://blog.janestreet.com/protocol-emulator-asic-competition/), which is the source
   of every requirement above.
2. [IHP Verilog template](https://github.com/TinyTapeout/ttihp-verilog-template), the required starting point.
   Its Actions run the tests, the GDS build and precheck.
3. [Tiny Tapeout](https://www.tinytapeout.com/), with its [local hardening guide](https://tinytapeout.com/guides/local-hardening/)
   (needed only past the 6-hour limit) and its [FPGA vs ASIC guide](https://tinytapeout.com/hdl/fpga_vs_asic/).
4. [Demo board](https://tinytapeout.com/specs/pcb-etr/) and [pinouts](https://tinytapeout.com/specs/pinouts/).
5. SRAM on CMOS5L: [Tiny Tapeout's SRAM test design](https://www.tinytapeout.com/chips/ttihp0p2/tt_um_urish_sram_test)
   and [Ken Pettit's walkthrough on adding an IHP SRAM macro](https://kdp1965.github.io/ihp-um-janestreet-prism/ihp_sram_macro.html).
6. [Tiny Tapeout Discord](https://discord.gg/rMmn8kXJng), which has a channel for this competition.

### Local tools: one download
1. [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build/releases/latest), about 503 MB for macOS arm64.
   It bundles Icarus Verilog, Verilator, Yosys, [cocotb](https://github.com/cocotb/cocotb),
   [sby](https://github.com/YosysHQ/sby) (formal proofs), eqy (equivalence checks), GTKWave and Surfer.
2. [KLayout](https://www.klayout.de/build.html), optional, for viewing the finished layout.

### Loop and evaluation
1. Claude Code, run headless with `claude -p`, is the worker.
2. [OpenEvolve](https://github.com/algorithmicsuperintelligence/openevolve) evolves code between `EVOLVE-BLOCK`
   markers against a scoring function. It is useful later for squeezing one module, not for the whole chip.
   [ShinkaEvolve](https://github.com/SakanaAI/ShinkaEvolve) does the same with fewer samples.
3. [VerilogEval](https://github.com/NVlabs/verilog-eval), [CVDP](https://github.com/NVlabs/cvdp_benchmark) and
   [RTLLM](https://github.com/hkust-zhiyao/RTLLM) measure an agent's general Verilog skill. They are a sanity
   check, not this project's judge.
4. [OpenHands](https://github.com/OpenHands/OpenHands) and [SWE-agent](https://github.com/SWE-agent/SWE-agent)
   are other general coding agents, in case a second worker is wanted.

### Optional
1. [Hardcaml](https://hardcaml.org/), the hardware language Jane Street uses. Any language is allowed, and plain
   Verilog fits the template and the agent best.

## Rules for agents working here

1. Never edit `judge/`, the reference models or anything in `../protoemu-holdout/`, and never read the holdout.
2. Run `judge/score.py` before every commit, and put the score in the commit message.
3. One change per round. Keep it if the score rose, revert it if not.
4. Every register gets a reset through `rst_n`. Nothing may rely on power-up values.
5. Before calling anything done, re-mark the requirements checklist with evidence.
6. Note how AI was used as you go. The write-up has to describe it.
