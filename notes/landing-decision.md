# Decision for Mike: landing the pin tests and the program fixes

Written by the main session on 2026-11-22. Mike answered it that morning.

## Mike's answer, 2026-11-22, about 09:19 PST

The main session asked, with the buttons "Yes, merge" and "Not yet":

> Merge fixes once GitHub tests pass, accepting 12 behaviour changes and 5 gaps left open for now?

The message above the question linked this page and said:

> **Say yes to the merge.** It fixes real bugs and can be reverted. It accepts the 12 behaviour changes, leaves
> five gaps open until Wed–Thu, and runs the chip-level test on GitHub instead of downloading files to your Mac.

Mike typed: **"merge"**.

The main session reads that as a yes to the three lines under "What 'Yes, land them' means", and to nothing more:

1. The five gaps are waived for this merge only, not for the submission. Changing a scenario or the RTL to close
   one still needs Mike's yes.
2. The 12 added behaviours are accepted, and the scenarios stay as written.
3. The local gate-level run (P4) is waived. Each branch still passes GitHub's tests, gate-level simulation
   included, before it reaches `main`.

It does not accept the write-up, pick the novel feature, approve a download or purchase, or change the golden
model, the repository's description or any old branch.

**Recommended answer: "Yes, land them."** It lets the UART, SPI and I2C requirement (R3) be met, and it changes no
scenario, reference model or RTL.

## What "Yes, land them" means

1. The five gaps below are waived for this merge. Each stays a failing known-gap test and a documented limit.
2. The 12 added behaviours below are accepted as the programs' documented behaviour. The scenarios stay as written.
3. The pin tests' gate-level run before pushing (the review's line P4) is waived. Both branches still have to pass
   gate-level simulation in GitHub on their own branches before either reaches `main`.

"Download first" means the same, except that line 3 is not waived: the main session downloads five files (5.2 MB)
and runs the pin tests on the routed netlist on Mike's Mac before pushing. To refuse one line, name it, for
example "no to gap 4" or "no to addition 9".

## The five gaps

| # | Gap | Why it is open | Recommendation |
|---|---|---|---|
| 1 | I2C has no plain read | Scenario 7 puts a write address before every read. | The scenario stays, and the limit is documented. |
| 2 | USB can't send a handshake packet | Scenario 11 puts a CRC-16 on every packet. | The scenario stays, and the limit is documented. |
| 3 | USB can't receive a handshake packet | Scenario 12 checks a CRC-16 on every packet. | The scenario stays, and the limit is documented. |
| 4 | Ethernet sends no link pulses | Scenario 13 wants both lines low when idle. | The scenario stays, and the limit is documented. |
| 5 | Ethernet receive takes 8 ns of jitter, not 13.5 ns | Sampling at 60 MHz can't guarantee more. | An RTL change is decided in the novelty brainstorm. |

## The 12 added behaviours, where the scenarios are silent

Additions 4 and 9 are the reviewer's two judgement calls. The recommendation is to accept all 12.

| # | Program | What it now does |
|---|---|---|
| 1 | UART receive | It drops a frame with a bad stop bit and reports `0x01`. The four-at-once demo drops it silently. |
| 2 | SPI controller | It stops the clock, with CS low, while the host is late. |
| 3 | SPI target | It keeps an unsent byte, reports an underrun with `0x01` and drops a byte that CS cuts short. |
| 4 | I2C | A read count of 0 ends after the write. Read literally, scenario 7 would still send a read address. |
| 5 | I2C | It reports `0x02` for a stuck bus and drops the rest of that request. |
| 6 | WS2812 | It uses newer pulse times inside the scenario's tolerance, a 282 us latch and `0x01` for a late byte. |
| 7 | 1-Wire | It reports a shorted bus as "no device". Bytes read before the short may already be with the host. |
| 8 | PS/2 | It reports `0xFF` for a bad start or stop bit and for a 250 us stall. |
| 9 | USB receive | It allows one extra bit before the end and stops passing on bytes after a bit-stuffing error. Scenario 12 says "every byte after SYNC", but tests no such packet. |
| 10 | USB transmit | It ignores an empty request, waits for the packet's first byte and flags an underrun with `0x01`. |
| 11 | Ethernet transmit | It ignores a length of 0, and it cuts and flags an underrun. |
| 12 | The UART 8E1 example | It reports a framing error's byte on the status channel. |

## What the evidence shows, and what it can't

1. The main session merged `e64d638` with `main` on Mike's Mac: 116 of 116 pin tests pass, the five gaps fail as
   intended, 14 of 14 benches pass and lockstep matches 323 of 323 traces. It also reproduced the three bugs that
   the review found, on the old programs, and their fixes.
2. No gate-level or GitHub run has happened on either branch.
3. No outside decoder has judged the waveforms, and the limits were read from secondary texts, not the standards.
4. The SPI target supports mode 0 only, and UART ships images for four baud rates. Others need a constant edited.
