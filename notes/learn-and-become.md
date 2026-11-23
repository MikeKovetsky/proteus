# Learn it, become it: the requirements

Written by the main session on 2026-11-22, before the build. The builder and the reviewer each mark every line
MET or NOT MET, with evidence.

## The sources

1. Mike asked what was left for a winning submission, then answered "Yes, run it" to the plan below.
2. What Mike said yes to, in the main session's words: "plug in an unknown device, and the chip questions it,
   writes its own program from the answers and replaces it." It also listed the limits of a first experiment:
   "The two largest didn't fit, and it missed a 16-step secret unlock code. It hasn't tried a real protocol or
   device." And: "our edge would be showing it working."
3. [The post](https://blog.janestreet.com/protocol-emulator-asic-competition/): "Show us anything else your
   architecture makes possible that we haven't thought of." A protocol emulator "is a useful tool for hardware
   debugging and reverse engineering, which is a good part of what we do." The write-up is judged on "how clearly
   it explains any new techniques or flows you introduce."

## A correction to what Mike was told

**The chip does not write its own program.** Its machines can't write their table, and the learner needs far more
memory than the chip has. In the experiment a Python program on the host chose the questions and wrote the
program. The chip asked the questions on the pins and then became the device. The demo board's microcontroller
could be that host, which is untested.

The main session asked Mike, with the buttons "Yes, that's fine" and "Let's discuss":

> Is it OK if a host computer, not the chip itself, writes the program for learned devices?

Mike answered on 2026-11-22: **"Yes, that's fine"**. So L2 reads: the host writes the program from the answers
that the chip got on the pins. Every description of the feature says that the host does it.

## The checklist

| # | Requirement, in the source's words | What shows it | Status |
|---|---|---|---|
| L1 | "plug in an unknown device, and the chip questions it" | The learner reaches a hidden device only through the chip's pins, and states what it must be told (wiring, bus, reset) | MET in simulation: `test/learn/test_isolation.py` holds the jail and the imports to it, and every result lists what had to be told (`needs` in `learn/host/session.py`) |
| L2 | "writes its own program from the answers" | The host writes it, from the chip's answers alone, which Mike accepted: "Yes, that's fine" | MET in simulation: the jailed host writes it from the chip's answers (`learn/lab/jail.py`, `test/learn/test_isolation.py`, `test/learn/test_end_to_end.py`) |
| L3 | "and replaces it" | The device is removed, its program is loaded through the pins into the RTL, and the original controller can't tell the two apart | NOT MET in full: met on RTL for both I2C devices at 400 kHz and for the flash at 2 MHz, with the unmodified SPI controller's chip clocked at 24 MHz (`learn/lab/replace.py`). At the SPI controller's shipped 5 MHz the judge finds the copy wrong, and an SPI copy can't watch nRESET |
| L4 | "It hasn't tried a real protocol or device" | Devices on two or more real protocols from the repo's eight in simulation, then a physical device once a board arrives | NOT MET: SPI and I2C devices work in simulation (`learn/lab/devices.py`). No physical device has been tried |
| L5 | "The two largest didn't fit, and it missed a 16-step secret unlock code" | A denser program, stronger search, a stated bound on what is guaranteed, and a refusal or warning beyond it, never a silent wrong copy | MET in simulation, with a weakness: the probe's two largest fit with their inputs told (300 and 461 entries), the 16-step code is found with its size told, and no copy came without a warning. But the warning is on every copy, and 39 of 160 blind devices were copied wrong, 7 of them outside the untraced secrets (`learn/results/families.jsonl`) |
| L6 | "anything else your architecture makes possible that we haven't thought of" | A like-for-like demonstration of what questioning finds that learning from a recording can't, with prior art credited | MET in simulation: hidden features found by questions 6 of 6 and 5 of 5, by the passive learner 1 of 6 and 0 of 5 at equal cost, and no advantage on the thermometer (`python3 -m learn baselines`). Prior art is credited on the page |
| L7 | "our edge would be showing it working" | One command reproduces the demonstration and its negative controls with open-source tools already in the repo's setup | MET: `python3 -m learn` ends `All as it must be` in 297 s on two cores of this Mac, with the OSS CAD Suite |
| L8 | "how clearly it explains any new techniques or flows you introduce" | A short page a newcomer can follow, with the limits. Its public claims wait for Mike | MET as a draft: `notes/learn-it-become-it.md`. No newcomer has tried it, and its claims wait for Mike |

No example may be a CAN, JTAG or SWD program or device, because those are the stranger test's protocols. No
scenario, reference model or frozen test changes.

## The reviewer's marks, 2026-11-22

A fresh Opus 5.5 reviewer, which had not built this, marked each line from the code, the tests and its own runs
on this Mac. The P lines are the rules in the brief it was given.

| # | Mark | Evidence |
|---|---|---|
| L1 | MET in simulation | `learn/lab/jail.py` gives the host a copy of `learn/host` and a pipe with two requests, load and exchange; `test/learn/test_isolation.py` shows ten ways out stopped by the jail and open without it, and identical traffic from two differently built devices that behave alike. Every result lists what had to be told (`needs` in `learn/host/session.py`) |
| L2 | MET in simulation | The jailed host writes the image from what it is told and the chip's answers. Every copy in the demonstration and in the tests is judged on RTL by `learn/lab/judge.py`, which shares no code with the host |
| L3 | NOT MET | As the builder says: the flash's copy replaces it only with the SPI controller's chip slowed from 60 to 24 MHz, so 2 MHz in place of 5. At the shipped 5 MHz the judge finds the copy wrong, though the device's own test passes there. An SPI copy can't follow nRESET. Needs more work or Mike's waiver |
| L4 | NOT MET | No physical device has been tried, and the devices are made up. Needs a board, or Mike's waiver until one arrives |
| L5 | MET in the letter, with an open finding | No silent wrong copy and no broken guarantee among the 160 blind devices and the 11 controls. `python3 -m learn evaluate`, run again in review, gave the kept row for all 160 devices, every field but the wall time. The probe's two largest are learned exactly through the pins with 300 and 461 entries (`test/learn/test_end_to_end.py`, added in review), and the 16-step lock is found with its size told. **Open:** the warning singles out no wrong copy. Its lines depend only on what was told and on counts (`warnings` in `learn/host/session.py`), so all 145 copies carry the same ones and 39 are wrong, among them every SPI register device with a bank switch (7 of 7). A fix changes `learn/host`, which voids the blind result. Mike's call |
| L6 | MET in simulation at short lengths; the long run not repeated | `python3 -m learn` in review: recordings of 1,000 and 10,000 transactions gave the passive learner 0 of 6 and 0 of 5 hidden features, against 6 of 6 and 5 of 5 from questions. The page's figures at equal cost (1 of 6, 0 of 5, and 0 of 6 and 2 of 5 at random) come from `python3 -m learn baselines`, which the review did not re-run. Prior art is credited |
| L7 | MET | `python3 -m learn` ended `All as it must be, in 323 s` with exit status 0, on two cores while another job held two more |
| L8 | MET as a draft | `notes/learn-it-become-it.md`. The review corrected five claims: what m can be, the seeds table, the register misses, the causes of the refusals and the list of refusals. No newcomer has tried it, and its claims wait for Mike |
| P1 | MET | Only `learn/`, `test/learn/` and `notes/` change. The 122 pin tests, the compiler's tests and lint pass. The tests went from 62 to 66, all passing |
| P2 | MET | No CAN, JTAG or SWD program, device or example in the changed files |
| P3 | MET | No claim of "first", "new" or "novel". The page credits Görgens's notes for the idea on this chip, and the methods to Angluin, Rivest and Schapire, Kearns and Vazirani, Fujiwara and others, and de Ruiter and Poll |
| P4 | MET | The page, the demonstration's output and these notes say that the host writes the program |

What the sources ask for that no line covers:

1. The post calls a protocol emulator "a useful tool for hardware debugging and reverse engineering". The host
   prints no readable account of the machine it learned, only counts and the program.
2. The demo board's microcontroller as the host. The page says the host has not run under MicroPython.
