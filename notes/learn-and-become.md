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
