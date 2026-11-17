# protoemu

An entry for Jane Street's
[protocol-emulator ASIC competition](https://blog.janestreet.com/protocol-emulator-asic-competition/): a small
programmable chip that speaks UART, SPI and I2C, and protocols it was not designed for. AI agents build it against
tests they cannot edit, and the chip goes out on Tiny Tapeout's IHP shuttle.

Status, 2026-11-16: the plan below replaced the first plan (an RTL hill-climbing loop) on the day it was written.
The architecture bake-off has started. No RTL exists.

## The competition, from the post (read 2026-11-16)

### Dates
1. Submissions close on 2027-01-18.
2. Selections are announced in early February 2027.
3. About four designs are taped out on the March 2027 IHP CMOS5L shuttle. Winners get their chip on a Tiny
   Tapeout demo board.
4. The final submission form will be added to the post closer to the deadline. It does not exist yet.

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
5. The post names the RP2040 PIO and TI Sitara PRU as inspiration, and asks what you would do differently.
6. The shape is open. A CPU-style design is the common approach, but an eFPGA, several engines sharing a pipeline,
   or something else is fine.

### Deliverables
1. A public repository with RTL, tests and a passing GDS and precheck.
2. Architecture and instruction set documentation.
3. An assembler or toolchain (or a bitstream flow) with example programs.
4. A verification write-up.
5. Optionally, a video or demo.
6. Program loading is part of the design. The demo board's microcontroller is the natural host, but any documented
   host works.
7. The post suggests testing the RTL on an FPGA before the ASIC flow.

### Judging
1. Flexibility leads: can the chip run protocols it was not designed for? The post does not say how the judges
   will test this.
2. The judges also weigh which protocols are shown, the architecture's unique abilities, and the design and
   verification approach.
3. The write-up is weighted heavily, including how readable it is and how clearly new techniques and flows are
   explained.
4. Fewer protocols with a strong architecture and verification can still compete.
5. AI use is allowed. The write-up must describe how AI tools were used.
6. Gate-level simulation is strongly recommended. Formal methods, constrained random tests and AI-assisted
   verification are named as interesting.
7. Novelty decides the tape-out. Jane Street pays to tape out the most novel designs, and asks for unique
   functionality and for novel design and verification methods.

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
| R14 | Novel: unique functionality, or a novel design and verification method | NOT MET | |

## Plan: simulator first, loop second

The first plan hill-climbed RTL from an empty design. Its gates scored every early round zero, so no round could
be kept, and it left the architecture, which the judges weigh most, to one line. This plan searches for the
instruction set where a trial takes seconds, builds RTL against the chosen model, and keeps the hill-climb for
area and timing.

### This week

Mike, 2026-11-16: "I want the results this week". The plan is cut by day, and each day ends in a result.

| Day | Result | Lines |
|---|---|---|
| Mon 2026-11-16 | This plan is written. The bake-off benches and three candidate simulators are started. | |
| Tue 2026-11-17 | The bake-off table is ready and Mike picks the architecture. The flow is proven: the template at 8x4, with an SRAM macro and a dummy design, passes GDS, precheck and gate-level simulation. | R1, R7 |
| Wed 2026-11-18 | The winning simulator is frozen as the golden model. The assembler, the loader and the RTL core pass lockstep tests. UART, SPI and I2C run on RTL. | R2, R3, R4, R5, R8, R10 |
| Thu 2026-11-19 | Every bake-off protocol runs on RTL. The reset and instruction proofs, mutation coverage and the sigrok check pass. The first GDS of the real design is built. | R12 |
| Fri 2026-11-20 | Timing closes at 60 MHz after place-and-route. The optimisation loop trims area and slack. Gate-level simulation passes on the real netlist. | R6, R7, R12 |
| Sat 2026-11-21 | The stranger test runs on unseen protocols. USB and Ethernet are handled on chip as far as the design allows. | R14 |
| Sun 2026-11-22 | The architecture and instruction set docs, the verification write-up with its AI-usage section, and the re-marked checklist are done. One commit is tagged as ready to submit. | R9, R11 |

1. **R13 cannot happen this week.** The submission form is not published yet.
2. **Two things can slip the week, and both are wall-clock.** A GDS run of a full 8x4 design may take hours
   (GitHub's limit is 6), and timing closure can need several runs.
3. **The weeks from 2026-11-23 to 2027-01-18** are for a second pass on whatever the week leaves weak, an FPGA or
   demo-board demo, and the submission.
4. **Mike's parts this week** are three: approve the tool downloads (Monday), pick the architecture (Tuesday), and
   decide what the write-up claims (Sunday).

### Layout (target)

```
/                   Tiny Tapeout IHP template: src/, test/, docs/, info.yaml, .github/workflows/
bakeoff/            the architecture bake-off: harness, benches and one simulator per candidate
model/              the winning simulator, frozen as the golden model
asm/                assembler and example programs
judge/              the scorer for the optimisation loop; the worker never writes here
loop/               the driver script and the ledger of rounds
notes/ai-usage.md   how AI was used, logged as the work happens
notes/lessons.md    what the loop has learned; the worker reads it every round
```

Held-out protocol specs and tests live outside the repo, in `../protoemu-holdout/`. They are never committed,
never listed here, and the worker can never read them.

### 1. Architecture bake-off (`bakeoff/`)

1. Three candidate architectures are written as cycle-accurate Python simulators, with no RTL. Each has an
   assembler and a program for every bench.
2. The candidates are:
   1. `cand_pio`: PIO-style state machines with shift registers and clock dividers. This is the baseline most
      entries will build.
   2. `cand_event`: an event-driven engine whose instructions wait on edges, timers and pattern matches, with
      configurable helpers for CRC, bit-stuffing, line coding and clock recovery.
   3. `cand_table`: a table-driven state machine with counters and shifters. It reacts in one cycle and is
      programmed by a compiled table.
3. The benches are 14 scenarios over 8 protocols: UART, SPI, I2C, WS2812, 1-Wire, PS/2, low-speed USB and 10 Mbit
   Ethernet. `bakeoff/README.md` holds the contract and every scenario.
4. The harness, not the candidate, models the pads: a two-flop synchroniser in and a registered output. Every
   candidate pays the same three cycles of round trip.
5. No hardware block may know a protocol by name. Helpers are generic and configured by the program.
6. The table reports, per candidate: benches passed, program sizes, the flop and memory budget, concurrency, and
   what could not be done and why. The budget is self-reported until RTL exists.
7. Mike picks. The winner's simulator becomes the golden model in `model/`.

### 2. Prove the flow

1. Bring in the template, set `info.yaml` to 8x4, and add an SRAM macro and a dummy design about the size of the
   real one.
2. Get GDS, precheck and gate-level simulation green in GitHub Actions.
3. Try local hardening. [Tiny Tapeout's guide](https://tinytapeout.com/guides/local-hardening/) uses LibreLane in
   Docker and quotes about 10 minutes. This is not confirmed for Apple Silicon or for a full 8x4 design. If it
   works, timing is checked several times a day and not once a night.

### 3. RTL against the golden model

1. Random programs and random pin input run on the model and on the RTL, and are compared cycle by cycle.
2. Agents build in ordinary sessions. Tests are gates: a commit never lowers the number of passing tests.
3. [sby](https://github.com/YosysHQ/sby) proves reset (R4) and the effect of each instruction.
4. [mcy](https://github.com/YosysHQ/mcy) mutates the synthesized netlist, and the tests must fail on the mutants.
   Tests that pass on a broken design are worthless.
5. Independent oracles check what the tests claim, because tests and RTL written by one author share its
   misreadings. [sigrok's decoders](https://sigrok.org/wiki/Protocol_decoders) read the simulation's waveform,
   and [cocotbext models](https://github.com/alexforencich/cocotbext-i2c) act as the other end of the bus. Sigrok
   has no Manchester or 10BASE-T decoder, so Ethernet needs another independent check.

### 4. The optimisation loop (`loop/run.sh`)

1. It runs only once the tests are frozen, so every round has a gradient.
2. The score is ordered, not weighted: all tests pass, then timing slack at 60 MHz, then area.
3. `claude -p` makes one change per round. The driver keeps the commit if the score rose and reverts it if not,
   and logs the round either way.
4. It stops at a fixed round cap. An unattended loop can burn a lot of tokens, so it runs on the Claude
   subscription or with a hard cap.
5. Every N rounds, a separate agent reads the ledger and rewrites `notes/lessons.md`.

### 5. The stranger test

1. A fresh agent gets only the published docs, the assembler and the spec of a protocol never used in this repo.
   It writes a program. The RTL is frozen at a tag.
2. The driver runs the program outside the worker's reach, and an independent decoder judges the waveform.
3. A final set of protocols runs once, at the end.
4. The write-up's headline is the count: N of M unseen protocols ran from the docs alone. The test checks the docs
   (R9) and the assembler (R10) too.
5. The limit: the stranger is the same model family as the builder. It shows the chip is programmable from its
   docs, not that it is easy for a person.

### Guards against a loop that games its score

1. The driver, not the worker, runs the judge: from a pinned commit, in a clean checkout, taking only `src/` and
   `asm/` from the worker. A round whose diff touches any other path is rejected.
2. The worker runs in [Claude Code's sandbox](https://code.claude.com/docs/en/sandboxing) with
   `../protoemu-holdout/` unreadable. Deny rules alone are not enough, because
   [they do not cover a script that opens files itself](https://code.claude.com/docs/en/permissions#what-read-and-edit-rules-cover).
3. Mutation coverage runs on a sample every round and in full every night, so weakening the tests cannot raise
   the score.
4. A human checks the tests themselves. In Jane Street's August ASIC puzzle, one solver's simulator matched the
   sample waveform while it silently turned off a check. Matching a sample is not proof.

### What stays human

1. **The architecture pick,** from the bake-off table.
2. **What counts as correct.** That means the bench scenarios, the reference models and the properties.
3. **The write-up.** The agent drafts it, and a person decides what it claims.

## The pieces

### Competition and flow
1. [The competition post](https://blog.janestreet.com/protocol-emulator-asic-competition/), which is the source
   of every requirement above.
2. [IHP Verilog template](https://github.com/TinyTapeout/ttihp-verilog-template), the required starting point.
   Its Actions run the tests, the GDS build and precheck.
3. [Tiny Tapeout](https://www.tinytapeout.com/), with its [local hardening guide](https://tinytapeout.com/guides/local-hardening/)
   and its [FPGA vs ASIC guide](https://tinytapeout.com/hdl/fpga_vs_asic/).
4. [Demo board](https://tinytapeout.com/specs/pcb-etr/) and [pinouts](https://tinytapeout.com/specs/pinouts/).
5. SRAM on CMOS5L: [Tiny Tapeout's SRAM test design](https://www.tinytapeout.com/chips/ttihp0p2/tt_um_urish_sram_test)
   and [Ken Pettit's walkthrough on adding an IHP SRAM macro](https://kdp1965.github.io/ihp-um-janestreet-prism/ihp_sram_macro.html).
   It needed Metal4 power stripes and DRC waivers.
6. [Ken Pettit's competing entry](https://github.com/kdp1965/ihp-um-janestreet-prism): a RISC-V core with a
   programmable state-machine engine, with CI for GDS, tests and FPGA. It is the bar for novelty.
7. [Tiny Tapeout Discord](https://discord.gg/rMmn8kXJng), which has a channel for this competition.

### Local tools
1. [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build/releases/latest), about 503 MB for macOS arm64.
   It bundles Icarus Verilog, Verilator, Yosys, [cocotb](https://github.com/cocotb/cocotb),
   [sby](https://github.com/YosysHQ/sby) (formal proofs), [mcy](https://github.com/YosysHQ/mcy) (mutation
   coverage), eqy (equivalence checks), GTKWave and Surfer.
2. Docker, for LibreLane hardening on this machine.
3. `sigrok-cli`, for the independent protocol decoders.
4. [KLayout](https://www.klayout.de/build.html), optional, for viewing the finished layout.
5. The bake-off needs none of these. It runs on the system Python 3.9 with the standard library only.

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

1. Never edit `judge/` or a frozen test, and never read or write `../protoemu-holdout/`.
2. Tests are gates. A commit never lowers the number of passing tests, and its message gives the count.
3. In the optimisation loop, make one change per round. The driver keeps it if the score rose and reverts it if
   not.
4. Every register gets a reset through `rst_n`. Nothing may rely on power-up values.
5. Before calling anything done, re-mark the requirements checklist with evidence.
6. Log how AI was used in `notes/ai-usage.md` as you go. The write-up is drafted from it.
7. Plan by day and by result. No milestone is dated weeks out.
