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
   planted fault that must fail. The self-test runs 180 checks. The main session re-ran it before trusting the
   report. The subagent flagged its own weak spot: in the Ethernet receive scenario only the 300-byte frame drifts
   enough to test clock recovery.
6. **First candidate.** A second subagent designed the table-driven candidate, its compiler and 14 programs in
   about 43 minutes, and reported 14 of 14 benches passing. The main session re-ran the benches and got the same.
   The candidate's hardware budget is its author's estimate. Its author named the top risk itself: one timing
   path that is not checked in synthesis.
7. **Tools and CI.** Claude installed the OSS CAD Suite after Mike approved the download, and proved it on a
   counter. Claude turned on GitHub Pages after Mike approved it. GitHub's docs and test workflows passed on the
   reviewed template, which confirms the reviewer's pinout fix on an independent run.
8. **What a person decided.** Mike asked for the critique, set the one-week pace, approved the 503 MB download and
   GitHub Pages, and will pick the architecture from the bake-off table.
