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
13. **What a person decided.** Mike asked for the critique, set the one-week pace, approved the 503 MB download and
    GitHub Pages, and picked the table-driven architecture from the bake-off table.
