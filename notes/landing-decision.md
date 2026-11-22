# Decision for Mike: landing the pin tests and the program fixes

Written by the main session on 2026-11-22. Mike answered it twice that morning, and the corrections made since are
marked as such.

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

**The main session's recommendation, written before Mike answered: "Yes, land them."** It lets the UART, SPI and I2C requirement (R3) be met, and it changes no
scenario, reference model or RTL.

## Mike's second answer, 2026-11-22, about 09:58 PST

The fixes' reviewer found that this page left out five behaviour changes. That was the main session's mistake: it
built the list from a review summary and didn't check it against the code. It then showed Mike these five:

| # | Case | Old programs | New programs |
|---|---|---|---|
| 13 | PS/2 in the four-at-once demo gets a broken frame | Passes the byte on as good | Drops it silently |
| 14 | The host stalls mid-packet on USB or Ethernet send | No warning | Flags `0x01`, then waits for the rest of that packet, so a host that starts a new packet instead loses it |
| 15 | The SPI target has nothing to send | The controller reads `0xFF` from a released pin | It sends `0x00` and flags it |
| 16 | The host never reads the status channel | No error | OVERFLOW can stay set with no data lost |
| 17 | A glitch hits USB receive | Reports nothing | Reports one bad, empty packet |

It then asked one question about the week's plan. The parts about this merge and about who decides are quoted, and
each […] is a part about other planned work:

> Shall I […] make small technical calls myself, starting with the bug fixes?

The message above the question said:

> It was whether five small error-handling changes in the bug fixes are OK. They are, and you shouldn't have to
> judge that, so I've replaced it with one question about the whole plan.
>
> **"Yes, run it" means** I […] merge the fixes including those five changes once tests pass, and make technical
> calls of that size myself. I'll still ask you before spending money, changing what the tests count as correct,
> making public claims or submitting.

Mike answered: **"Yes, run it"**.

So the five changes are accepted, 17 in all, and small technical calls are the main session's, to be logged.
Spending, what the tests count as correct, public claims and submission are still Mike's, as are downloads and
deletions, which the answer doesn't mention.

**A technical call made under that answer, after a correction to row 15.** When a reply is queued after CS falls
and before the first clock, the old SPI target sent it. The new one sends `0x00` with `0x01` and stays one byte
behind. The fixes' reviewer found this, and the main session reproduced it and kept the new timing: on the golden
model the old one sent a nearly late `0x21` as `0xA1` with no status. The deadline is in `docs/info.md`, tested from
both sides.

**Two corrections found after Mike answered, replayed on the golden model by the main session.** Both make the old
programs worse than the table says. In row 15, `0xFF` holds only for a transaction with nothing queued from its
start: short in the middle of one, the old program sent `0x00` with no status. In row 17, the old program didn't
report nothing: after a glitch of 1, 10 or 40 clocks it passed on `0xFF` as data for as long as it was watched,
102 bytes in 550 us, with no status.

In the tables below, gaps 2 and 3 also cover USB token packets, and addition 5's stuck-bus limit is 25 ms.

## What happened next

The pin tests reached `main` as `a60b878` and the fixes as `539d19e`, each after a full review and after GitHub's
tests passed on that commit, gate-level simulation included: 28 of 28, then
[122 of 122](https://github.com/MikeKovetsky/protoemu/actions/runs/45060257274) on the routed netlist.

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

## What the evidence showed when Mike was asked, and what it couldn't

1. The main session merged `e64d638` with `main` on Mike's Mac: 116 of 116 pin tests pass, the five gaps fail as
   intended, 14 of 14 benches pass and lockstep matches 323 of 323 traces. It also reproduced the three bugs that
   the review found, on the old programs, and their fixes.
2. No gate-level or GitHub run has happened on either branch.
3. No outside decoder has judged the waveforms, and the limits were read from secondary texts, not the standards.
4. The SPI target supports mode 0 only, and UART ships images for four baud rates. Others need a constant edited.
