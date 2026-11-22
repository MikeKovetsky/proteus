# Learn it, become it

**This is a draft. It is not for publishing, and its claims wait for Mike.** Everything below was measured in
simulation. The devices were made up for this, and nothing has run on hardware.

Wire an undocumented SPI or I2C device to the chip. A host program chooses questions, and the chip puts them to
the device on the pins and brings back the answers. From those answers alone the host works out the device's
state machine and **writes a program for the chip**. You take the device away, load that program, and the chip
answers the device's controller in its place, within the limits below.

The chip does not write the program. A Python host does, on a computer.

## Try it

```
source ~/oss-cad-suite/environment
python3 -m learn
```

Run it from the repository's root. It takes about five minutes and needs no hardware. It learns three devices, puts
each copy in front of a shipped controller, compares questioning with learning from a recording, and runs eleven
negative controls. It runs two heavy processes at a time (`LEARN_JOBS` changes that, and no result). It ends
with `All as it must be` and exit status 0.

## How it works

```
  1. ask        host ---questions---> chip ---pins---> device
                host <----answers---- chip <---pins--- device
  2. learn      host: which states are there, and what does each one answer and do?
  3. doubt      host: test suites hunt for a wrong answer. One found goes back to step 2.
  4. write      host: the state machine becomes entries in the chip's table of 512.
  5. become     controller <---pins---> chip, with the learned program. The device is gone.
```

A **question** is a reset and then a sequence of bus events: select, a byte, deselect, or on I2C a START with an
address, a byte, a read, a STOP. A **state** is all that decides what the device will answer later, with where
the bus is in a transaction counted as part of it. Two histories lead to one state when no follow-up question
tells them apart. Active automata learning finds those states by asking.

## What you must tell it, or give it

1. The bus, SPI mode 0 or I2C, and wiring as the host prints it.
2. The rate to question at and the rate to serve, unless the defaults will do.
3. A reset line that brings the device back to the same state every time, and how long it needs.
4. The bus to itself, and leave to change the device. **Questions can write, erase or lock a real device.**
5. Optionally, the most states the device has, or the only inputs that matter. Either tightens what is promised,
   and the second narrows it to those inputs.

It is told nothing of what the device does. The host runs in a separate process whose one way out is a link to
the chip, and the tests check that. That guards against a leak by accident, and is no wall against hostile native
code.

## What it promises, and what it can't

With n states found, for a device that is deterministic, is cleared by reset and changes only with whole bus
events:

1. If the device has at most n states, the model is exact for every event the bus allows.
2. If it has at most n + m states (m is at least 1, and is printed), and every event that was not learned in full
   acts like that kind's default, the model is exact.
3. If the inputs were told, the model is exact for those inputs up to n + m states, and says nothing of others.

**Beyond that, a copy can be wrong, and the warning can't tell you whether yours is.** It is on every copy. No
test through the pins can find every secret: a long enough code that shows nothing until its last step is just
one sequence among too many. The host refuses when it sees noise, time-outs, a reset that doesn't clear, a size
bound that it can't afford to test, or a machine that won't fit. The promise is about the model. The program
comes from a generator that is tested and not proven.

## What it did

| Device | States | Table entries | Bus events asked | Chip time | Exact, by a separate judge |
|---|---|---|---|---|---|
| Flash-like SPI part with modes, a test mode and a hidden key | 51 | 170 of 512 | 2.6 to 3.6 million | 6 to 9 s | 6 of 6 seeds |
| I2C sensor with a hidden register and a hidden address | 64 | 364 of 512 | 1.2 to 1.3 million | 25 to 28 s | 3 of 3 seeds |
| I2C thermometer with nothing hidden | 13 | 140 of 512 | about 130,000 | 3 s | 6 of 6 seeds |

The judge alone sees the truth. It walks every reachable pair of a true state and a state of the whole chip's
RTL, and tries every bus event from each. For the replacement, the repo's controller program runs unmodified
against device and copy: the test written for the device and 300 random transactions got the same answers from
both. The I2C controller runs at its own 400 kHz. The SPI controller's chip is clocked at 24 MHz in place of 60,
which makes its 5 MHz program a 2 MHz one. The same test fails on an empty bus and on copies with one wrong byte.

The feasibility probe's two largest machines, of 54 and 90 states, didn't fit. Carried to SPI with the same
graphs and their inputs told, they are learned exactly through the pins and take 300 and 461 entries.

## What questioning finds that a recording doesn't

The recordings hold normal traffic, error cases and stray bytes, hidden ones not excluded. Each is as long as
the questioning was. The passive learner is a state merger that is helped with the alphabet, the bus phases and
the defaults. Both baselines are scored on the test written for the device.

| Device | Questions: hidden features | Recording: hidden features | Random questions: hidden features |
|---|---|---|---|
| Flash-like part | 6 of 6, exact | 1 of 6, not exact | 0 of 6, not exact |
| Sensor | 5 of 5, exact | 0 of 5, not exact | 2 of 5, not exact |
| Thermometer | None to find, exact | None to find, **exact as well** | None to find, **exact as well** |

Where nothing is hidden, questioning showed no advantage at equal cost. A recording of normal traffic lacks
hidden behaviour almost by definition, and that is the point: one has to ask.

## How often it fails on devices it wasn't tuned on

The host was frozen at commit `a0acf17` on the builder's local branch, which was rebuilt on `main` with the same
code as `be3896e`. The seeds still come from `a0acf17`'s hash, and the freeze check compares the frozen files'
contents, so the rebuild changes neither. Ten families of devices were then written by the same AI agent, seeded
from that commit's hash, committed, and run once (`python3 -m learn evaluate` repeats it).

| Outcome | All 160 | Without the 32 untraced secrets |
|---|---|---|
| Exact copy | 106 | 106 |
| Refused, no program | 15 | 15 |
| **Wrong copy, with the general warning** | **39** | **7** |
| Broken promise, copy with no warning, or crash | 0 | 0 |

The 32 untraced secrets were built to lie outside the promise, and all were missed. The other seven are the
weakness that matters most: **an ordinary SPI register write**, a command and a value that show nothing until a
later read, can be missed. The refusals are devices where whole ranges of bytes act alike, which the table
encodes badly.

## Other limits

1. An SPI copy serves up to about 3 MHz with the flash's command set, and less with more commands. **At the
   shipped controller's 5 MHz the judge finds the flash's copy wrong**, though the device's own test passes there.
2. An SPI copy has no input left for the reset line, so resetting it means reloading it. An I2C copy honours it.
3. Copies differ in timing. Flash: MISO follows CS falling by 100 to 117 ns against the device's 50. I2C: SDA
   follows SCL falling by 317 to 350 ns against 340.
4. It doesn't copy stored data, timers, analog behaviour, answers within a byte, or other buses.
5. The host avoids the syntax MicroPython is known to lack and held about 200 kB at most alive in CPython.
   **It has not been run under MicroPython** or on the demo board.

## Credit

1. Dana Angluin, *Learning regular sets from queries and counterexamples* (1987): L*, the method.
2. Ronald Rivest and Robert Schapire, *Inference of finite automata using homing sequences* (1993): the search
   within a counterexample. Michael Kearns and Umesh Vazirani (1994): the classification tree.
3. Fujiwara and others, *Test selection based on finite state models* (1991): the Wp-method behind the promise.
4. Protocol state fuzzing applies the same learning to protocol implementations, as in Joeri de Ruiter and Erik
   Poll, *Protocol state fuzzing of TLS implementations* (2015).
5. **The idea of doing this on this competition's chip was published before this work**, in Matthias Görgens's
   protocol-emulator notes:
   [brainstorm-wild.md](https://github.com/matthiasgoergens/protocol-emulator-notes/blob/master/brainstorm-wild.md),
   item 18, and
   [codex-brainstorm-2026-09-25.md](https://github.com/matthiasgoergens/protocol-emulator-notes/blob/master/notes/codex-brainstorm-2026-09-25.md).
   No code was taken from it or from any other entry.

## Claims that wait for Mike

1. May the write-up say "learn it, become it", always with "the host writes the program" and "in simulation"?
2. May it publish the failure table above as the headline evidence, wrong copies included?
