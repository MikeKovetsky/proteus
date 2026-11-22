# protoemu

An entry for Jane Street's
[protocol-emulator ASIC competition](https://blog.janestreet.com/protocol-emulator-asic-competition/): a small
programmable chip meant to speak UART, SPI and I2C, and protocols it was not designed for (R3 is not met, and the stranger test has not run). AI agents build and review
it with tests as gates, and the chip goes out on Tiny Tapeout's IHP shuttle.

Status, 2026-11-21, 22:30 PST: **resumed by Mike** ("resume until all requirements are met"), after a pause
from 2026-11-17, 07:35 PST ("pause, document the progress and push everything"). At 22:55 they set the order:
**"let's complete without novelty first and then right after it brainstorm how to make it novel"**. The
bake-off is done, and Mike picked the table-driven architecture. Its RTL core matches the golden model on every recorded clock,
the real chip, with both SRAMs, passes Tiny Tapeout's whole flow with setup, hold, slew and capacitance limits met at 60 MHz (max-fanout warnings remain), and the architecture
and instruction set docs are written. "Progress" below has the detail, and "Open work" says
where each open piece stands.

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
| R1 | Built from the IHP Verilog template, 8x4 tiles set in `info.yaml` | MET | Template 4b1204a brought in by f917341; `info.yaml` sets `tiles: "8x4"` |
| R2 | Reprogrammable after fabrication, within its timing and I/O limits | MET in simulation; timing is R6 | `test/test.py` loads five different programs through the pins after reset |
| R3 | "Start with UART, SPI and I2C", with "example programs for the protocols you support"; "standard settings (common UART baud rates, SPI modes 0-3, I2C at 100 and 400 kHz) are a good target" | NOT MET, reopened 2026-11-21 | The shipped programs pass their benches and have real bugs the pin tests found: an I2C write with a read count of 0 reads 65,536 bytes and then reports success, the SPI target loses the byte queued for its next transaction, and UART receive accepts a 0 stop bit (all three reproduced on the golden model). There are no programs for SPI modes 1 and 2, and I2C runs at about 360 kHz with no 100 or 400 kHz program |
| R4 | Known state from `rst_n`, with no reliance on power-up values | MET | All 1,172 flops reset asynchronously (`test/synth/generic.ys` checks only the cell types, which does not prove freedom from power-up values); the loader writes all 512 table entries; the sby reset proof is not done |
| R5 | Program reload after deselect works | MET in simulation | `test_reset_reload_run_again` in `test/test.py`; it reloads after a reset, deselection itself is not simulated |
| R6 | Timing closes after place-and-route at the chosen clock (60 MHz target) | MET | [Run 44998474402](https://github.com/MikeKovetsky/protoemu/actions/runs/44998474402) at 16.6 ns, the slew fix's build, on extracted parasitics: worst setup slack 4.0960 ns (slow corner), worst hold slack 0.0955 ns (fast corner), and 0 setup, hold, slew and capacitance violations in all three corners, so timing at the SRAM pins is no longer extrapolated. 97 max-fanout warnings remain and do not fail the build |
| R7 | GDS builds and precheck passes in Tiny Tapeout's GitHub Actions | MET for the real design | [Run 44076197046](https://github.com/MikeKovetsky/protoemu/actions/runs/44076197046) on `1a24c4a`: gds, precheck ("Precheck passed"), gl_test and viewer all pass; 0 routing DRC errors, LVS "Circuits match uniquely", 0 antenna violations. All four jobs passed again in [run 44997274424](https://github.com/MikeKovetsky/protoemu/actions/runs/44997274424) on `9e640fb`, with the compiler fix and unchanged layout inputs. With the slew fix, [run 44998474402](https://github.com/MikeKovetsky/protoemu/actions/runs/44998474402) passes gds, precheck and gl_test, with 0 routing, LVS and antenna violations; its viewer job is skipped by design off `main`. The flow's waivers are unchanged (`src/config.json` says why for each). It must pass again on every design change |
| R8 | RTL and tests in the public repo | MET | On `main` from 2026-11-17 |
| R9 | Architecture and instruction set documentation | MET | `docs/architecture.md` (the machine and why), `docs/isa.md` (every field, operation and language form), `docs/programming.md` (a new protocol step by step); every example run; a check on 2026-11-21 found overstated claims, the false ones are corrected and the rest wait for the open branches to land |
| R10 | Assembler or toolchain with example programs | MET | `bakeoff/cand_table/compiler.py`, its bench programs (identical images before and after the fix of 2026-11-21) and the worked 8E1 UART in `docs/examples/`. The silent miscompiles found while writing the docs and in the fix's full review are closed: 63 of 63 compiler tests (`python3 -m bakeoff.cand_table.test_compiler`), and the reviewer replayed 3,008 compiled programs on the RTL. CI does not run those tests yet ("Open work", 8). The programs' own bugs are R3 |
| R11 | Verification write-up, including how AI was used | NOT MET | `WRITEUP.md` and three decisions for Mike (`notes/writeup-claims.md`) are on the local branch `writeup`. It makes no novelty claim, marks each unfinished result as pending, and lands once Mike accepts |
| R12 | Gate-level simulation passes | MET | The 7 pin tests pass 7 of 7 on the routed netlist in run 44076197046's gl_test job, and again in [run 44997274424](https://github.com/MikeKovetsky/protoemu/actions/runs/44997274424) with programs built by the fixed compiler and in [run 44998474402](https://github.com/MikeKovetsky/protoemu/actions/runs/44998474402) with the slew fix |
| R13 | Submitted before 2027-01-18 | NOT MET | |
| R14 | Novel: unique functionality, or a novel design and verification method | NOT MET, deferred by Mike to a brainstorm right after the other lines | The novelty check of 2026-11-21 found prior art for every main idea: TejasDasa's entry published a bake-off of PIO, state-table and register-machine models on 2026-09-16 and uses a state table |
| R15 | "Your submission should be open source so others can use and build on it." (found missing from this list on 2026-11-21) | MET | The repository is public and `LICENSE` is Apache 2.0 |

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

### Progress, 2026-11-17, 07:35 PST

1. **Tools.** The OSS CAD Suite is installed. A counter passed Verilator lint, an Icarus simulation, a cocotb test,
   Yosys synthesis and an sby proof.
2. **Template (R1).** It is in at 8x4 tiles and 60 MHz, reviewed and pushed. The review named the placeholder's
   pins, because Tiny Tapeout's docs check fails on a blank pinout, and widened the test until it failed on seven
   deliberately broken designs.
3. **GitHub Actions (R7 met for the placeholder).** The docs and test workflows pass on every push.
   [The first GDS build](https://github.com/MikeKovetsky/protoemu/actions/runs/44025802951) passed all four jobs
   on the placeholder at 8x4: the layout took 50 minutes, then precheck 18 minutes, the gate-level test 46 seconds
   and the viewer 15 seconds, 68 minutes in all. The [layout viewer page](https://mikekovetsky.github.io/protoemu/)
   is live. Other 8x4 entries' GDS builds take 2.5 to 3.7 hours, so the real design will take longer than the
   placeholder. Every push starts a new GDS build, so builds whose layout inputs did not change get cancelled.
4. **Benches.** All 14 scenarios have a bench. The self-test passes 191 of 191 checks: each bench passes a
   hand-written fake chip that does the scenario correctly and fails fakes with one planted fault each, and the
   reference CRCs match outside vectors. The review tightened five benches (UART framing, I2C START and STOP
   setup, the USB packet with no data, and how long four benches watch after the expected end).
5. **Bake-off.** All three candidates pass 14 of 14 on the tightened benches, re-run by the main session. The
   table under "1. Architecture bake-off" has the numbers. Mike picked `cand_table`.
6. **SRAM timing.** IHP's timing file for the 512x32 SRAM gives 6.4 ns from clock to data out at the slow corner
   (1.08 V, 125 °C) and 3.8 ns at the typical corner. That leaves about 10 ns of a 16.7 ns clock for the decision
   logic. It is a datasheet figure, not synthesis. The CMOS5L SRAM folder in IHP's PDK is a link to these files.
7. **RTL.** The table machine is in Verilog (`src/protoemu_*.v`). The core matches the golden model on every clock it replays: 323
   of 323 traces, 3,051,548 clocks, from all 14 benches and 300 random programs, replayed in Verilator in about 5
   seconds (`make -C test lockstep`). The suite catches 21 of 21 planted bugs (`make -C test mutants`), and its
   reviewer reported planting 64 more, all caught unless harmless, but kept no list or script of them. Seven pin-level
   tests pass, including reset, reload and run again. Yosys gives 1,172 flops (budget 1,202) and about 2,700 generic
   gates (3,310 multi-input cells after CI's mapping). An estimate on IHP's cells puts the decision loop at 9.3 ns of the 16.6 ns clock before wires; it is not
   independently confirmed, and the flow's timing report is the real answer.
8. **SRAM layout setup.** The two SRAMs go through the flow with Ken Pettit's placement, power stripes, antenna
   diodes and DRC waivers, proven on a dummy design in simulation. The power stripes come from a LibreLane
   plugin, and LibreLane's docs say plugins are not supported in its Docker container; the first layout build
   settled it, because both plugin steps ran in Tiny Tapeout's container.
9. **Integration.** Both are merged with `main` on the branch `rtl-sram-flow`, reviewed, and pushed. Its
   [first layout build](https://github.com/MikeKovetsky/protoemu/actions/runs/44047118911) met setup timing (slack
   1.12 ns at the slow corner), but detailed routing ended with 382 violations (369 shorts) and LVS failed. Pettit's SRAM
   spots left the channel between the two pin edges open at one end only, and Metal3 is the tile's only
   horizontal routing layer, so the 244 nets leaving the channel had 345 tracks; global routing overflowed and
   sent 25 nets straight over the SRAMs. The fix moves both SRAMs to x 273.12 (6 stripe pitches right, so the
   power stripes land as before) and spreads them to the core's bottom and top edges, which opens the channel
   at both ends with 759 Metal3 tracks each.
   [The second layout build](https://github.com/MikeKovetsky/protoemu/actions/runs/44076197046) passed every
   job in 42 minutes: detailed routing reached 0 violations in 8 minutes (2 h 26 min before), LVS matches,
   precheck passes, and the 7 pin tests pass on the routed netlist. Worst setup slack is 4.20 ns at the slow
   corner, up from 1.12 ns, because the detour nets went away. Utilisation is 24.6%.
10. **The other two candidates** are reviewed and pushed. The review fixed ten problems and held every number in
    their READMEs to a check, which moved `cand_event`'s program total from 1,548 to 1,580 bytes. Both still pass
    14 of 14.
11. **`main`'s own layout build** of the real chip passed all four jobs
    ([run 44105853045](https://github.com/MikeKovetsky/protoemu/actions/runs/44105853045), 26 minutes).
12. **Docs (R9).** `docs/architecture.md`, `docs/isa.md` and `docs/programming.md` are on `main`, with a worked 8E1
    UART in `docs/examples/` that runs on the golden model. A light review fixed the example's decoder, which had
    stopped looking 11 bits before the end of the capture, and three wrong claims in the guide. The docs name no
    program for CAN, JTAG or SWD.

### Open work

**The order.** Mike, 2026-11-21: "let's complete without novelty first and then right after it brainstorm how to
make it novel". The main session's reading of it:

1. First, every line but R14 is finished: the compiler fix, the electrical fix, the pin tests, the proofs, the
   docs' remaining overclaims, and the write-up with no novelty claim. R13 also waits for Jane Street's form.
2. Right after, Mike and the main session brainstorm R14. The architecture pick and the project's name are open
   there. The table stays until then.
3. The stranger test and its bench review wait for the brainstorm, because the test runs once on frozen RTL and
   the brainstorm may change the RTL. R14 is deferred, not waived.

1. **The slew fix: landed on 2026-11-22.** It puts a buffer at each of the 216 live SRAM pins, fails the build on
   slew, capacitance or antenna violations, and lets only `main` deploy the public viewer from that workflow. Its
   [layout build](https://github.com/MikeKovetsky/protoemu/actions/runs/44998474402) met the bar: 0 slew and 0
   capacitance violations in all three corners, with setup, hold, routing, LVS, precheck and gate-level simulation
   clean. It replaces the first attempt, `electrical-fix`, whose
   [green build](https://github.com/MikeKovetsky/protoemu/actions/runs/44111759734) still broke both limits and
   deployed over `main`'s viewer. That branch and `rtl-sram-flow` still carry the old viewer job, so neither is to
   be merged, pushed or re-run. Deleting them waits for Mike's yes.
2. **The compiler fix: landed on 2026-11-21.** Its full review added four commits, closed 14 silent miscompiles
   beyond the five bugs the docs found, and left nothing on the checklist open. The language is stricter: a line
   that sets something twice is refused. Every shipped program compiles to the image it did before.
3. **The stranger test's benches (`../protoemu-holdout/stranger/`, never committed by design), waiting for the
   brainstorm.** Their review was paused on 2026-11-21, before it reported or exported anything. One of its runs
   left output in the session's temp folder, against its instructions: that reviewer removes it on resuming,
   no other agent opens it, and the stranger's sandbox denies that whole folder. The pieces are a scenario sheet
   (`SCENARIOS.md`) and benches for CAN, JTAG and SWD, with reference codecs, behavioural fakes and `run.py`,
   which judges a compiled image on the golden model. The self-test passes 77 of 77 checks. The agent was stopped
   while checking one hex constant in the sheet, so its README may be unfinished. Not reviewed. Before the test:
   Mike reads `SCENARIOS.md` (what counts as correct stays human), a reviewer checks the benches (Mike allowed
   one on 2026-11-21: rule 1 under "Rules for agents working here"), then the
   stranger runs, locked out of the folder and of every agent transcript by the OS sandbox, with only `SCENARIOS.md`, `docs/` and the compiler.
4. **The pin tests (branch `pin-tests`, 9662164, in full review).** All 14 scenarios run through the pins, 27 of
   27 tests pass on RTL, and `test/known_gaps.py` holds 17 failing reproductions of program gaps. They have not
   run on the routed netlist. Their timing limits were typed from a model's memory and need checking against
   the standards.
5. **The program fixes (branch `program-fixes`, on the pin tests).** UART, SPI and I2C come first (R3), then the
   gaps in USB, Ethernet, 1-Wire, PS/2 and WS2812. Whatever needs an RTL change is reported, not worked around.
6. **The proofs (branch `formal`).** The reset and instruction proofs, the host link in `src/project.v`, and
   mutation coverage are being built.
7. **Not started.** An independent decoder check (sigrok is not installed), and local hardening, which needs a
   container engine and is not needed while CI builds in about 40 minutes.
8. **CI runs too little.** It runs the pin tests, docs and layout, and not the compiler's tests, lockstep, the
   bake-off or the self-checks. They go into `test.yaml` next, now that the slew fix, which edits that file, has landed.

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

Held-out protocol specs and tests live outside the repo, in `../protoemu-holdout/`. The specs and tests are committed only after their test has run,
only the protocols' names are listed here, and the worker may never read them.

### 1. Architecture bake-off (`bakeoff/`)

1. Three candidate architectures are written as cycle-accurate Python simulators, with no RTL. Each has an
   assembler and a program for every bench.
2. The candidates are:
   1. `cand_pio`: PIO-style state machines with shift registers and clock dividers. This is the familiar
      baseline.
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
7. Mike picks. The winner's simulator becomes the golden model. For now it stays in `bakeoff/cand_table`.

Results (2026-11-16, 22:50 PST, on the tightened benches). The budgets are the candidates' own figures, not
synthesized.

| | `cand_pio` | `cand_event` | `cand_table` |
|---|---|---|---|
| Benches passed | 14 of 14 | 14 of 14 | 14 of 14 |
| Flops (of about 3,000) | 1,134 | 1,296 | 1,202 |
| Combinational cells | about 7,000 | about 4,800 | about 2,600 |
| SRAM bits | 32,768 | 32,768 | 32,768 |
| Protocols at once | 4 at 30 MHz, or 2 at 60 MHz | 2 at full speed | 4 at 15 MHz, or 1 at 60 MHz |
| All 14 programs | 1,949 bytes | 1,580 bytes | 4,138 bytes |
| Receive sweeps beyond the benches | 1,160 runs, 0 failed | 1,160 runs, 0 failed; Ethernet receive works at 1 of 3 sample positions | 270 runs, 0 failed (reported by the reviewer, with no script kept) |
| Weakest point | Closest to RP2040 PIO | Slow on someone else's clock: its SPI target passes at 4.3 MHz and fails at 4.6 MHz | Large programs, and 4 protocols share one decision rate |

**Decision.** Mike, 2026-11-16: "Table-driven". The reasons given to him: novelty decides the tape-out and the
other entries found build PIO-style machines; it decides on every clock; 13 of its 14 programs passed the first
time they ran; and it has the smallest logic. The alternative offered was `cand_pio`, the most robust and least
novel.

**Correction, 2026-11-21.** Two of those reasons do not hold. TejasDasa's entry and Ken Pettit's PRISM are both
state-machine engines, so "the other entries found build PIO-style machines" was false, and no record shows that
13 of the 14 programs passed the first time. "Tests they cannot edit", which this file and the README said until
today, was not true either: no mechanism enforced it. An earlier entry is also named ProtoEmu
([b3elz3bub/ProtoEmu](https://github.com/b3elz3bub/ProtoEmu), created 2026-09-19), so the name is Mike's to keep
or change. Lockstep builds the core alone, so only the pin tests check the host link in `src/project.v`.

`cand_table`'s open risk, by its author's account: it decides on every clock only if one path (memory output,
condition select, memory address) closes in 16.7 ns. It has since met setup and hold after layout, inside the
slew and capacitance limits. Had it failed, the Ethernet scenarios would have been lost. IHP's SRAM timing (Progress, item 6) leaves about 10 ns
for that logic.

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

**The protocols.** Mike, 2026-11-17: "yes", to CAN, JTAG and SWD. The competition post suggests all three, and none
is among the 14 bake-off benches. The roles below are the main session's defaults.

| Protocol | The chip's role | What it exercises |
|---|---|---|
| CAN 2.0A (classic frames, 11-bit identifier) | A node that sends a frame and receives one, with a bench node that answers in the ACK slot | Bit stuffing in both directions, CRC-15, the ACK slot, a shared open-drain bus |
| JTAG (IEEE 1149.1) | The controller (a probe): it drives TCK, TMS and TDI and reads TDO from a bench TAP | Walking the TAP state machine, shifting IR and DR of chosen lengths |
| SWD (Arm Serial Wire Debug) | The host: it drives SWCLK and reads and writes SWDIO against a bench target | Request parity, turnaround on a bidirectional pin, the 3-bit ACK, data parity |

**How it runs.**
1. A scenario sheet per protocol (pins, rates, host messages, what must happen), in the style of
   `bakeoff/README.md`, is fixed before the stranger starts. The sheet, `docs/` and the compiler are all the
   stranger gets.
2. The benches that judge the programs are written by another agent from the sheets alone. They live in
   `../protoemu-holdout/`, the stranger never sees them, and they are committed only after the test has run.
3. No doc, example or bench in this repo may show a program for CAN, JTAG or SWD before the test runs, so the
   stranger cannot copy one. Public entries do show them, so the stranger has no web access and gets neither
   this file, the write-up nor any agent's transcript.
4. The stranger's programs run on the golden model, which the RTL core matched on every recorded lockstep clock, and the benches judge
   the waveforms. An independent decoder (sigrok's CAN, JTAG and SWD decoders) is a second judge if
   `sigrok-cli` is installed.

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
2. **What counts as correct.** That means the bench scenarios, the reference models and the properties. No review of them by a person is recorded yet.
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
   coverage), eqy (equivalence checks), GTKWave and Surfer. Release 2026-11-16 is installed at
   `~/oss-cad-suite` (1.8 GB, checksum verified). `source ~/oss-cad-suite/environment` puts it on the path. Its
   Python is 3.11 with cocotb 2.1.
2. Docker, for LibreLane hardening on this machine. It is not installed.
3. `sigrok-cli`, for the independent protocol decoders. It is not installed, and neither is Homebrew.
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

1. Never edit `judge/` or a frozen test, and never read or write `../protoemu-holdout/`. One exception: asked
   "May one reviewer agent open protoemu-holdout to check the benches and export the scenario sheet?", Mike
   answered "Allow one reviewer" on 2026-11-21. That one agent may read and run what is there and export the
   sheet. It may not write there or tell any other agent what the benches hold, and no other agent may open the
   folder, the main session included.
2. Tests are gates. A commit never lowers the number of passing tests, and its message gives the count.
3. In the optimisation loop, make one change per round. The driver keeps it if the score rose and reverts it if
   not.
4. Every register gets a reset through `rst_n`. Nothing may rely on power-up values.
5. Before calling anything done, re-mark the requirements checklist with evidence.
6. Log how AI was used in `notes/ai-usage.md` as you go. The write-up is drafted from it.
7. Plan by day and by result. No milestone is dated weeks out.
