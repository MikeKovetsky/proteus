# How AI was used

A dated log, written as the work happens. The verification write-up's AI section is drafted from it (R11).

## 2026-11-16

1. **Critique of the first plan.** Claude Code (Claude Fable 5.2) re-read the competition post and six references,
   and found that the first plan's loop could not start: a failed gate scored zero, a round was kept only if the
   score rose, and an empty design fails the core-protocol gate. It also found that deny rules do not stop a
   Python script from reading the held-out tests, and that the post names novelty as the bar for tape-out.
2. **New plan.** Claude proposed designing in a simulator first and keeping the hill-climb for area and timing.
   Mike set the pace: "I want the results this week". Claude rewrote the plan by day.
3. **Bake-off set-up.** Claude wrote the contract between benches and candidates (`bakeoff/harness/`) and the 14
   bench scenarios (`bakeoff/README.md`). Four subagents were started in parallel: one writes the benches, and
   three each design one candidate architecture, its assembler and its programs. The candidate agents were told
   not to read each other's directories.
4. **Template and its review.** Claude brought in Tiny Tapeout's IHP Verilog template at 8x4 tiles and 60 MHz,
   with the template's placeholder adder as the only RTL until the architecture is picked. A fresh Claude reviewer
   (Opus 5.5) got the diff and the requirements but not the builder's reasoning. It read Tiny Tapeout's check code
   and CI logs, and found that the docs workflow fails when every pin in `info.yaml` is blank. It also broke the
   design on purpose and found that the template's one-sum test passed on four of seven broken versions. It named
   the placeholder's pins and widened the test until all seven failed.
5. **Benches.** One subagent wrote 13 of the 14 benches from the scenario spec, without reading any candidate. For
   every bench it also wrote fake chips: one that does the scenario correctly and must pass, and several with one
   planted fault that must fail. The self-test ran 180 checks. The main session re-ran it before trusting the
   report. The subagent flagged its own weak spot: in the Ethernet receive scenario only the 300-byte frame drifts
   enough to test clock recovery.
6. **First candidate.** A second subagent designed the table-driven candidate, its compiler and 14 programs in
   about 43 minutes, and reported 14 of 14 benches passing. The main session re-ran the benches and got the same.
   The candidate's hardware budget is its author's estimate. Its author named the top risk itself: one timing
   path that is not checked in synthesis.
7. **Tools and CI.** Claude installed the OSS CAD Suite after Mike approved the download, and proved it on a
   counter. Claude turned on GitHub Pages after Mike approved it. GitHub's docs and test workflows passed on the
   reviewed template, which confirms the reviewer's pinout fix on an independent run.
8. **Review of the bake-off.** A fresh Claude reviewer (Opus 5.5, max effort) got the diff, the scenario spec and
   the requirements, not the builders' reasoning. It found eight problems. Five benches were too lenient: a short
   UART stop bit, a 2-cycle start pulse, and an I2C START 1 cycle after SCL rose all passed, and so did a USB
   packet with no CRC. It proved each by planting the fault in the candidate's own programs. It also found that
   the compiler evaluated program constants with `eval`, which matters once another agent writes programs for the
   stranger test, and a stale decode cache after a reload. It fixed all eight and added a planted-fault case for
   each new check (191 checks, up from 180).
9. **The other two candidates.** The event-driven candidate's agent reported 14 of 14. The PIO-style agent stopped
   on the Fable 5.2 usage limit, which resets 2026-11-20 at 2pm Pacific, while updating its README. Its work was
   complete. The main session re-ran all three on the tightened benches: 14 of 14 each.
10. **SRAM timing.** The main session read IHP's published timing file for the SRAM all three candidates rely on,
    to replace a guess about the decision loop with a datasheet number.
11. **Models.** Until the Fable limit, the builders ran on Fable 5.2 and the reviewers on Opus 5.5. From
    2026-11-16, 22:30 PST, Mike switched the session to Opus 5.5, so builders and reviewers now run on the same
    model.
12. **Review of the other two candidates.** A fresh Opus 5.5 reviewer at max effort was told these candidates had
    lost the pick, so what mattered was that their reported results were true. It found ten problems: the
    event-driven model let helpers act one cycle earlier than its own spec and budget allow, its assembler ran
    `eval()`, and three of its programs hung on a count of zero. The PIO-style candidate's checks always exited 0,
    and its assembler silently truncated fields that were too wide. Several README numbers were unbacked or wrong.
    It fixed all ten, held every README number to a check that fails when the number is edited, and proved each
    new check fails on a broken version. Both candidates still pass 14 of 14. The main session re-ran every check.
13. **RTL of the table machine.** A builder subagent (Opus 5.5) wrote `cand_table` in Verilog (`src/`), with IHP's
    SRAM simulation model, and a lockstep suite: the golden model records its 14 benches and 300 random programs,
    and the RTL core replays them in Verilator and must match on every clock (3 million clocks). It also wrote 21
    planted bugs the suite must catch, cocotb tests through the pins, and a decision-loop delay estimate on IHP
    cells (9.3 ns of the 16.6 ns clock, before wires). A fresh reviewer (Opus 5.5, max effort) got the diff and the
    requirements, not the builder's reasoning. It planted 64 more bugs in the core, and the suite caught every one
    that changes behaviour. In `project.v`, which the lockstep suite does not reach, 7 of 14 planted bugs passed
    every test, among them a synchroniser stage too few or too many. It added two pin-level tests that catch five
    of them; the other two are a metastability hazard and a race only a host breaking the protocol can reach. It
    also ran the pin tests on a netlist of generic gates: 7 of 7 pass. The builder then ran them on the netlist
    Yosys maps to IHP CMOS5L cells, with IHP's cell models: 7 of 7 pass. That is before layout, so it is not R12.
14. **SRAM layout setup.** A second builder subagent (Opus 5.5) built a dummy two-SRAM design and the layout flow
    config from Ken Pettit's public entry, which passes the same flow with the same SRAM, and tested it on RTL and
    on a local post-synthesis netlist with planted faults. A fresh Opus 5.5 reviewer restored Pettit's
    antenna-diode step, which the builder had dropped on a false reason, and added a check that both SRAMs' A_DLY
    pins are tied high, because the PDK model only calls `$stop`, which the simulator runs past under cocotb. The
    builder found that LibreLane's docs say plugins are not supported in its Docker container, and Tiny
    Tapeout's GDS action runs LibreLane in one; the first CI run settles it.
15. **Integration.** The main session re-ran both builders' checks, then merged the two branches and main on
    the branch `rtl-sram-flow`, following the layout builder's hand-over note, and renamed the SRAM instances in
    the layout config to where the RTL puts them.
16. **Review of the merge.** Both branches had their own full reviews, so a fresh Opus 5.5 reviewer at max effort
    read only what the merge changed and whether the two halves fit. It found that deleting the dummy design had
    also deleted the only test that both SRAMs' A_DLY pins are tied high: with one tied low, the pin tests still
    passed 7 of 7. It moved the check into the real pin tests and proved it fails on three bad ties, on RTL and on
    two gate-level netlists. It also restored a lost note in info.yaml, and checked the renamed SRAM instances
    against how Yosys, OpenSTA and LibreLane name flattened instances, replaying the power-connection patterns in
    tclsh. The main session re-ran the pin tests on the reviewed tip before pushing the branch.
17. **First layout build.** It failed in detailed routing, and a subagent (Opus 5.5) diagnosed it from the
    build's logs, reports and DEFs with its own scripts, since the flow cannot run locally: all 382 shorts came
    from 11 nets that global routing had laid over the SRAMs, because the one open end of the channel between
    their pin edges had fewer Metal3 tracks than nets leaving it. It weighed the candidate fixes against that
    evidence (a pin-edge keepout and tying the byte mask high did not address the channel's exit), moved the
    SRAMs to open both ends, and checked the new floorplan's geometry against the SRAM LEF. Two fresh Opus 5.5
    reviewers checked the numbers; the first suggested x 273.12, which keeps the macros on Pettit's site and
    track grid as well as his stripe grid. Only the next layout build can show that routing converges.
18. **Second layout build.** The main session pushed the reviewed floorplan fix, and the build passed every job.
    It checked the result against the build's own logs and metrics, not only GitHub's green mark: 0 routing
    violations, LVS "Circuits match uniquely", "Precheck passed", 7 of 7 gate-level tests, and 4.20 ns of
    setup slack. It also recorded the slew, capacitance and fanout warnings the flow does not gate on.
19. **Electrical warnings.** A subagent (Opus 5.5) traced all 78 slew, 1 capacitance and 97 fanout warnings from
    the layout build's reports: every slew violation is on an SRAM input pin, and 82 of the fanout warnings are
    clock-leaf buffers with fast edges. It found the cause in the flow's step order (the only design repair ran
    before hold buffers and antenna diodes doubled the SRAM nets' fanout) and in OpenROAD's and OpenSTA's source,
    and proposed four LibreLane settings. A light review passed it. The main session checked that each setting
    exists in LibreLane 3.1.0.dev4 before spending a layout build on it.
20. **Architecture docs.** A subagent wrote docs/architecture.md, isa.md and programming.md, checked every claim
    against the code and ran every example. It found eight places where the old bake-off README was wrong (for
    example 1,202 flops where synthesis gives 1,172) and five compiler bugs, three of which compile a different
    program from the one written. It was told not to use CAN, JTAG or SWD as examples. A light review found a
    real bug in the example's own decoder, which ignored the last 11 bits of the capture.
21. **Compiler fix.** A subagent fixed the five bugs by what one table entry can encode, kept all 19 images
    byte-identical, and fuzzed 60,000 mutated programs, which found two more crashes. Its full review was stopped
    when Mike paused the work.
22. **Held-out benches.** A subagent wrote the stranger test's scenario sheet and benches for CAN, JTAG and SWD
    outside the repo, from the sheet alone, each proven against faulty fake chips (77 checks). It was stopped at
    the pause.
23. **What a person decided.** Mike asked for the critique, set the one-week pace, approved the 503 MB download and
    GitHub Pages, picked the table-driven architecture from the bake-off table, and chose CAN, JTAG and SWD for
    the stranger test. On 2026-11-17 at 07:30 PST he paused the work: "pause, document the progress and push
    everything".
24. **The resume.** On 2026-11-21 Mike wrote "resume until all requirements are met". The main session first
    re-checked the pause note against GitHub and the post. The electrical fix's green build had missed its bar
    (slew and capacitance violations remain, which only warn), R10 went back to NOT MET until the compiler fix
    lands, and the checklist gained R15, the post's open-source rule, which the first reading had left out.
25. **The held-out folder.** Mike allowed one reviewer agent to open it ("Allow one reviewer", 2026-11-21) to
    check the stranger test's benches and export the scenario sheet. It reads and runs but does not write there,
    and reports only a verdict and counts, so what the benches hold reaches neither the builders nor the stranger.
26. **The novelty check.** The write-up's drafting agent searched public entries and prior art, and contradicted
    the project's own record: a reason the main session had given Mike for the architecture pick was false, the
    README said tests could not be edited when they could, and another entry had already published a similar
    bake-off. The main session checked each of those at its source, corrected them, and left the pick, the name
    and the write-up's claims to Mike.
27. **The order of work.** On 2026-11-21 Mike wrote "let's complete without novelty first and then right after it
    brainstorm how to make it novel". The main session paused the bench reviewer and had the write-up reworked
    to stand without a novelty claim or the stranger test.
28. **What the benches had missed.** An agent wrote pin-level tests from each protocol's definition, sharing no
    code with the benches, and found program bugs that all 14 benches had passed. The main session reproduced
    three of them with its own script on the golden model and moved R3 from MET back to NOT MET.
29. **The compiler's review.** A fresh Opus 5.5 reviewer at max effort found 14 more silent miscompiles in a fix
    for five, wrote a differential oracle from docs/isa.md, killed 77 of 77 mutants of the fixes and replayed
    3,008 compiled programs on the RTL. The main session replayed the reviewed chain onto main, re-ran the
    checks and probed the bugs with its own small programs before pushing.
30. **Corrections to this log and to the docs.** The write-up's agent checked every public claim against the code
    and proposed 52 corrections, and the main session applied those that touch only docs. The main session was
    wrong that the I2C write "never ends": run to completion on the golden model, it reads 65,536 bytes in
    1.644 seconds and reports success. Three earlier entries here overstate: the first layout's 382 "shorts"
    were 369 shorts and 13 spacing errors, the 64 extra planted bugs were reported with no list or script kept,
    and "checked every claim" against the code was the docs agent's report, which later checks contradicted.
31. **A correction to entry 30.** Entry 30 says the write-up's agent checked every public claim. The agent
    reported that it did not: it checked the claims it collected for the write-up and the passages around them,
    and did not read every public document in full. Other overstatements may remain in the public docs.
32. **The slew fix's review.** A fresh Opus 5.5 reviewer at max effort found that the new layout check could pass
    on a pre-layout estimate, that antenna violations failed nothing, and that the main-only rule for the public
    viewer never applied. It fixed all three before any layout was built. The main session confirmed the viewer
    rule in GitHub's settings, replayed the reviewed patch onto main, re-ran the checks and started the build.
33. **The slew fix's build.** The main session judged the reviewed fix by GitHub's logs: the report and LibreLane's
    own checkers show no slew, capacitance, setup or hold violation in any corner. It also traced two warnings the
    gate does not read. The power-grid failure precedes the SRAM stripes and the final check finds the grid
    connected, and the parasitic names the IR-drop step missed are diodes on the SRAMs' unused test pins.
34. **The slew fix on main.** The main session checked main's own build by the same bar: the same slacks, 0 slew,
    capacitance, setup and hold violations, precheck and 7 of 7 gate-level tests. GitHub's deployment record and
    the live site's header show that the viewer was deployed from that commit.
35. **The pin tests' review, and a wrong brief.** A fresh Opus 5.5 reviewer at max effort found gap tests that
    passed on a dead chip, a test that failed a correct WS2812 program and one more I2C gap. It did not mark the
    branch, because nothing had shown the suite working on the routed netlist. It also found that the program-fix
    agent had changed an existing test's expectation. The main session's brief had asked for that behaviour
    without Mike's decision, so the main session ordered it reverted and left the decision to Mike.
36. **The program fixes and CI.** An AI firmware agent closed 13 of the 18 known gaps in programs alone. Its audit
    found more changed expectations than the one the main session caught, and it took all of them back by
    replaying the work on the reviewed tests. For Ethernet jitter it built two frames that no receiver sampling at
    60 MHz can tell apart, instead of tuning the program to the test. The main session re-ran the suites,
    compared every gap test's body by syntax tree and left the five open gaps to Mike. It also put the compiler,
    model and lockstep suites into CI on a branch. Both changes went to fresh Opus 5.5 reviewers at max effort.
37. **The CI suites' review.** A fresh Opus 5.5 reviewer at max effort found that the main session's new CI job
    would have failed on every push: it never installed cocotb, which the test Makefile needs, and the main session
    had only run the commands on a Mac that has it. The main session reproduced the failure. The reviewer fixed it,
    replaced a guard test that 17 planted faults got past and made each suite prove that it can fail. The main
    session landed the chain after GitHub's own test and layout runs passed on the branch.
38. **The program fixes' review.** A fresh Opus 5.5 reviewer at max effort found three program bugs that 103 pin
    tests had passed, and 15 of its 65 planted bugs that those tests missed. It fixed the bugs and tightened the
    tests. It did not mark the branch, because only Mike can waive the five open gaps. The main session reproduced
    each bug on the old programs, re-ran the suites merged with main and wrote the decision on one page for Mike.
39. **The proofs.** An AI agent wrote an executable reading of the docs, proved the RTL and two netlists against it
    and tested the proofs with planted bugs and netlist mutants. The proofs found four false doc sentences and
    three golden-model bugs that the tests and earlier reviews had missed. The main session re-ran the proofs,
    replayed the model bugs, showed the lost byte with a pin test of its own, measured the turn order and latency
    on the model and corrected the docs. The proofs went to a fresh Opus 5.5 reviewer at max effort.
40. **Mike's landing decision.** Mike said they didn't know how to answer the two open questions. The main session
    reduced them to one, whether to merge the fixes with 12 added behaviours and five gaps open for now, and Mike
    answered "merge". The main session recorded the question, the answer and its limits in
    `notes/landing-decision.md`, for the two original reviewers to check their open findings against.
41. **The pin tests landed.** An agent built them, and a fresh Opus 5.5 reviewer at max effort fixed and marked
    them. The main session pushed the merge to its own branch, checked that 28 of 28 passed on the routed netlist
    in GitHub's log, checked Mike's recorded "merge" against the transcript, and only then pushed it to `main`.
42. **A hole in the pin tests.** The proofs' reviewer found from netlist mutations that the pin tests miss broken
    chips. The main session reproduced the largest case on RTL: with P4 to P7 dead, all 28 tests pass. A fresh
    agent was given the requirement in the project's words, "the tests must fail on the mutants", to fix it.
43. **Five changes the decision page left out.** The fixes' reviewer found that the page built by the main session
    omitted five behaviour changes. Mike then accepted them and left small technical calls to the main session,
    keeping spending, what counts as correct, public claims and submission. The record is in
    `notes/landing-decision.md`.
44. **A regression the review caught.** The fixes' reviewer found that the SPI target lost a reply queued between
    CS and the first clock. The main session reproduced it, compared both timings, kept the safer one as a small
    technical call and had the deadline documented and tested from both sides. The review also showed that two of
    the five changes had understated the old programs' faults, which the main session replayed and corrected.
45. **The program fixes landed.** An agent wrote them and fresh Opus 5.5 reviewers checked them, the last at max
    effort. The main session pushed the reviewed merge to its own branch, read GitHub's logs for 122 of 122 tests
    on RTL and on the routed netlist, and only then fast-forwarded `main` to it. No person reviewed the code.
46. **A plan for what is left.** Mike asked what was left for a winning submission. The main session tested a
    candidate feature on the golden model, found that a rival had published the idea, and asked one question about
    the plan. Mike answered "Yes, run it". While writing the requirements, the main session found that it had
    overstated the feature: the host, not the chip, writes the learned program.
47. **The host writes the learned program.** The main session told Mike about that overclaim and asked whether a
    host computer may write the program. Mike answered "Yes, that's fine", and the requirement now says so.
48. **Learn it, become it.** An Opus 5.5 agent built the feature from `notes/learn-and-become.md`: the learner, its
    test suites, the two table generators, a judge that alone sees the truth, the baselines and the demonstration.
    It improved the host on three of its negative controls, then froze it (a0acf17), committed ten seeded device
    families (4eb2196) and ran them once. The result was 106 exact, 15 refused and 39 copied wrong with a warning.
    It kept that result and left the host frozen. A scratch fix tried afterwards repaired 4 of the 7 unintended
    misses in a simulation without RTL, and it was not committed, since it was made looking at those devices. The
    same agent wrote the host, the devices, the families and the judge, so none of this is independent. The main
    session rebuilt the work on `main` without its private notes, with the code unchanged.
