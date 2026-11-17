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
4. **What a person decided.** Mike asked for the critique, set the one-week pace, and will pick the architecture
   from the bake-off table.
