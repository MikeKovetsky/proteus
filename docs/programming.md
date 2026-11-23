# Writing a protocol for proteus

This guide writes a protocol that none of the 14 bench programs uses, a UART with an even-parity bit (8E1), from
the spec to a program that runs on the chip's model and is checked by a decoder written for the example. Then it shows
the patterns the bench programs use for the harder parts of a protocol: exact bit loops, edges with time-outs,
clock recovery and CRCs.

You need Python 3.9 or later and this repository; every command runs from the repository's root. Keep
[isa.md](isa.md) open for the exact rules. [architecture.md](architecture.md) explains the machine.

## 1. The workflow

1. Read the protocol's spec and write down, bit by bit, what happens on each wire and when.
2. Choose the pins (P0 to P7), the host channels, and the clock counts. The chip runs at 60 MHz, so a
   microsecond is 60 clocks.
3. Write the program, one line per clock, in a `.fsm` file.
4. Compile it and read the listing.
5. Run it on the model with a script that plays the other end of the wire and judges the pins with its own
   decoder, not with anything the program computes.
6. Compile it to an image and load it into the chip.

## 2. Seven rules of timing

Most mistakes break one of these. [isa.md, "How a machine steps"](isa.md#1-how-a-machine-steps) has the detail.

1. **One line is one clock.** With n machines it is one turn, n clocks, and every count in the program counts
   turns.
2. **A line's actions all happen together, at the end of its clock.** They read the registers as they were at
   its start: `TX=d, shift` sends the bit that was at the end of S, then shifts.
3. **Rules do not see their own line's actions.** `K-=1 => kz: done` tests K before the decrement. Test it on
   the next line, or on a `hold` line.
4. **`hold n` lasts n clocks.** Its actions happen on the first and its rules are tested on the last, so from
   n = 2 the rules see the actions. The `crc` flag needs n = 3 (rule 7).
5. **Inputs arrive 2 clocks late and outputs leave 1 clock late.** A line that sees an input change is 2 clocks
   after the change on the wire, and a pin it sets changes 1 clock after the line. A pin operation gated with
   `when` answers an input in 3 clocks; a rule that moves to a line that sets the pin answers in 4.
6. **A waiting line (`wait`, `halt`) may only set pins.** "Do this, then wait" is two lines.
7. **The `crc` flag is one turn behind the LFSR.** Put at least one line between the last LFSR step and the
   line that tests `crc`.

## 3. The spec: UART 8E1

1. The line idles high.
2. A frame is a start bit (0), 8 data bits least significant first, a parity bit and a stop bit (1): 11 bits.
3. The parity bit is even parity: it makes the number of 1s in the data bits and the parity bit together even,
   so it is the XOR of the 8 data bits.
4. At 115,200 baud a bit is 60,000,000 / 115,200 = 520.83 clocks. The program uses 521, 0.03% slow, well
   inside a UART's tolerance.
5. The transmitter sends P0, and the host gives it bytes on channel 0. The receiver reads P1, pushes each good
   byte to channel 1, and pushes a byte whose parity is wrong, or whose stop bit is 0, to channel 2 instead.

The parity comes from the LFSR. With `poly=1`, a step XORs the bit into LFSR bit 0 and leaves the other bits at
0, so bit 0 is the running parity, and a pin can send it with `c`.

## 4. The transmitter

[docs/examples/uart_8e1_tx.fsm](examples/uart_8e1_tx.fsm):

```
machine tx
    out   TX=P0
    order lsb                        # the data bit d is S[0], so a byte goes out LSB first
    crc   poly=1                     # a 1-bit LFSR: each step XORs one bit into bit 0, the running parity
    const BIT = 60000000 / 115200    # clocks per bit; the compiler rounds it to 521

idle:   TX=1, wait rx0               # the line idles high until the host has given a byte
        S=pop0, K=8, crc=0           # the byte, 8 bits to go, parity 0
        TX=0, hold BIT               # start bit
bit:    TX=d, shift, crc+=d, K-=1, hold BIT => kz: parity | bit
parity: TX=c, hold BIT               # even parity: the XOR of the 8 data bits, read from the LFSR
        TX=1, hold BIT => idle       # stop bit
```

Line by line:

1. `machine tx` starts the program's only machine. `out TX=P0` names logical output 0 and maps it to P0.
   `order lsb` makes `S[0]` the data bit `d`. `crc poly=1` configures the LFSR. `const BIT` is a number the
   compiler works out and rounds.
2. `idle` drives the line high and stays until `rx0` says channel 0 holds a byte. A waiting line may only set
   pins, so the pop is the next line.
3. The next line takes the byte into S, sets K to 8, and clears the parity, all in one clock: one bus move, one
   counter load and one LFSR operation, which is what one line allows.
4. The start bit drives 0 for `BIT` clocks.
5. `bit` does four things in its first clock: it drives the data bit, shifts S so the next bit comes to `S[0]`,
   adds the same bit to the parity, and counts it. The data bit is read before the shift, so `TX=d` and
   `crc+=d` both see the bit being sent. The line lasts `BIT` clocks, and its rules are tested on the last of
   them, after `K-=1`: after the eighth bit K is 0 and the line goes to `parity`, otherwise back to `bit`.
6. `parity` drives LFSR bit 0, the XOR of the eight bits, for one bit time.
7. The stop bit drives 1 for one bit time and returns to `idle`.

A frame is 11 x 521 clocks. With bytes waiting back to back, `idle` and the pop line add 2 clocks to each stop
bit.

## 5. Compile it and read the listing

```
python3 -m bakeoff.cand_table.compiler docs/examples/uart_8e1_tx.fsm
```

```
docs/examples/uart_8e1_tx.fsm: 10 states in 13 entries (0 unused), image 123 bytes
  0  tx.idle                TX=1                                     -> 0 | rx0 [idle line12]
  1  tx.line12              S=pop0, crc=0, K=8                       -> 12 [line13]
  2  tx.line13.hold         -                                        -> 2 | tz [line13.hold bit]
  3  tx.bit                 TX=d, shift 0, crc+=d, K-=1, T=519       -> 4 [bit.hold]
  4  tx.bit.hold            -                                        -> 4 | tz,kz [bit.hold bit bit.hold parity]
  5  tx.bit                 TX=d, shift 0, crc+=d, K-=1, T=519       -> 4 [bit.hold]
  6  tx.bit.hold            -                                        -> 4 | tz,kz [bit.hold bit bit.hold parity]
  7  tx.parity              TX=c, T=519                              -> 8 [parity.hold]
  8  tx.parity.hold         -                                        -> 8 | tz [parity.hold line16]
  9  tx.line16              TX=1, T=519                              -> 10 [line16.hold]
 10  tx.line16.hold         -                                        -> 10 | tz [line16.hold idle]
 11  tx.idle                TX=1                                     -> 0 | rx0 [idle line12]
 12  tx.line13              TX=0, T=519                              -> 2 [line13.hold]
```

What it shows:

1. Each `hold BIT` line became two states: the line, which loads `T=519` (521 - 2), and a `.hold` state that
   waits for the timer. Unlabelled lines are named after their source line.
2. Entry 4 is the bit loop's decision. Its block, entries 4 to 7, holds the four successors in the order
   "neither", `tz`, `kz`, both: keep waiting, next bit, keep waiting, parity. `bit` and `bit.hold` are copied to
   fit, which is why 10 states take 13 entries.
3. The image is 123 bytes: 1 for the machine count, 16 of configuration, 2 for the entry count and 8 per entry.
4. Entry 7 sends LFSR bit 0, `TX=c`, where `bit` sends the shifter's bit, `TX=d`.

A program the compiler refuses prints `file:line: message` and the line, and the command exits with status 1.
[isa.md, "Limits and error messages"](isa.md#6-limits-and-error-messages) lists every message.

## 6. The receiver

[docs/examples/uart_8e1_rx.fsm](examples/uart_8e1_rx.fsm):

```
machine rx
    in    RX=P1
    order lsb                        # bits enter at S[7] and move down: the first one ends in S[0]
    crc   poly=1 match=0             # the running parity; data and parity bit XOR to 0 when they agree
    const BIT = 60000000 / 115200    # 521 clocks per bit

idle:   wait !RX                     # the start bit's falling edge
        K=8, crc=0
        hold BIT + BIT/2 - 2         # to the middle of data bit 0
bit:    shift RX, crc+=RX, K-=1      # take one bit into the shifter and into the parity
        => kz: parity                # a rule sees K as the clock began, so test it one line later
        hold BIT - 2 => bit          # a bit is BIT clocks: 1 + 1 + (BIT - 2)
parity: hold BIT - 2                 # to the middle of the parity bit
        crc+=RX                      # the parity bit joins the sum
        hold BIT - 1                 # to the middle of the stop bit; the crc flag has caught up by then
        => RX & crc: good            # the stop bit must be 1 and the parity right
        push2=S                      # an error: the byte goes to channel 2
        wait RX => idle              # and the line must be high before the next start bit
good:   push1=S => idle
```

The receiver samples each bit in its middle. Count from the clock e on which the start bit's falling edge
reaches the pin:

1. `wait !RX` sees the low on clock e + 2, because of the synchroniser, and leaves.
2. `K=8, crc=0` runs on e + 3, and the hold starts on e + 4.
3. `bit` runs on e + 4 + H, where H is the hold, and sees the pin as it was on e + 2 + H.
4. The middle of data bit 0 is 1.5 bits after the edge, e + 781. So H = 1.5 x BIT - 2, which is
   `hold BIT + BIT/2 - 2`.
5. From one sample to the next is `bit` (1 clock), the `kz` test (1 clock) and `hold BIT - 2`: one bit.
6. After the eighth bit, `parity` waits `BIT - 2` clocks, so `crc+=RX` samples the middle of the parity bit.
7. One bit after `crc+=RX` the test sees the middle of the stop bit and, at once, whether the parity was
   right. The `crc` flag compares the LFSR with `match` one clock late, and the hold is far longer than that.
8. A stop bit of 0 is a framing error. `wait RX` then keeps `idle` from taking the line, still low, for the next
   start bit, and a break is reported once.

The receiver never sends, so it declares no outputs.

## 7. Two protocols at once

The transmitter uses P0 and channel 0, and the receiver P1 and channels 1 and 2, so both can run in one image,
one machine each. Two machines take turns on the table, so each steps every second clock and counts in turns
of 2 clocks. The only change is to halve the bit time, in both files' `const BIT` lines:

```
    const BIT = 60000000 / 115200 / 2    # 260 turns, 520 clocks
```

The receiver's "- 2" for the synchroniser now counts turns, which moves its sample point by a clock or two out
of 520, well inside the bit. The script in the next section builds this image by joining the two files and
changing that line, and checks both directions at once.

## 8. Run it on the model

```
python3 docs/examples/run_uart_8e1.py
```

It should print this, in under a second, and exit with status 0:

```
tx: 10 states in 13 entries, a 123-byte image
rx: 17 states in 22 entries, a 195-byte image
duplex: 27 states in 36 entries, a 323-byte image
tx: sent 00 55 a7 ff 31 80, decoded 00 55 a7 ff 31 80 from P0
rx at +2%: channel 1 got 12 c3 7e 80 (wanted 12 c3 7e 80), channel 2 got 5a 2d (wanted 5a 2d)
duplex at +2%: sent 00 55 a7 ff 31 80, decoded 00 55 a7 ff 31 80 from P0
duplex at +2%: channel 1 got 12 c3 7e 80 (wanted 12 c3 7e 80), channel 2 got 5a 2d (wanted 5a 2d)
rx at -2%: channel 1 got 12 c3 7e 80 (wanted 12 c3 7e 80), channel 2 got 5a 2d (wanted 5a 2d)
duplex at -2%: sent 00 55 a7 ff 31 80, decoded 00 55 a7 ff 31 80 from P0
duplex at -2%: channel 1 got 12 c3 7e 80 (wanted 12 c3 7e 80), channel 2 got 5a 2d (wanted 5a 2d)
uart_8e1: ok
```

[run_uart_8e1.py](examples/run_uart_8e1.py) does this:

1. It compiles both programs, and the two-machine image of section 7.
2. For the transmitter, it gives channel 0 six bytes, runs the model, and decodes P0 with its own 8E1 decoder:
   the start, parity and stop bits must be right, every edge must sit on the bit grid within 2%, the line must
   stay high until 11 bits after the start edge (within the same 2%), and no frame may be left over after the
   six bytes.
3. For the receiver, it drives P1 with six frames, one with a wrong parity bit and one with a stop bit of 0,
   first 2% fast and then 2% slow. The four good bytes must come out on channel 1 and the bad ones on channel 2.
4. It fails if the chip and the test ever drive a pin to opposite levels.

The checks fail on broken programs. Each of these changes makes the script fail: `TX=!c` for the parity bit, no
`crc+=d`, `K=7`, a stop bit of half a bit, `match=1` in the receiver, no `crc+=RX` for the parity bit, no `RX &`
in the last test, `nop` instead of `wait RX`, and a first hold of `BIT - 2`.

### Writing your own script

The smallest useful script compiles a program, gives it a byte and prints the pin's edges:

```python
import sys
sys.path.insert(0, ".")                       # run from the repository's root
from bakeoff.cand_table.chip import TableChip
from bakeoff.cand_table.compiler import compile_file
from bakeoff.harness.sim import Sim, edges

result = compile_file("docs/examples/uart_8e1_tx.fsm")
sim = Sim(TableChip(), result.image)          # every pin pulled high
sim.send(0, b"\x41")                          # the host gives channel 0 one byte
sim.run(7000)                                 # 7,000 clocks at 60 MHz
print(edges(sim.wave, 0))                     # every change of P0 as (clock, level)
```

It prints `[(0, 1), (19, 0), (540, 1), (1061, 0), (3666, 1), (4187, 0), (5229, 1)]`: the start bit falls on
clock 19, and every edge after it is a whole number of 521-clock bits later. 0x41 has two 1s, so the parity bit
is 0: the line falls for bit 7 at 4187 and stays low through the parity bit until the stop bit at 19 + 10 x 521.

1. Drive an input with `sim.set(pin, level)` before each `sim.tick()`, as `run()` in run_uart_8e1.py does.
2. Read the host's side with `sim.received(1)` and `sim.received(2)`.
3. Judge the pins with a decoder written from the spec, never from the program.

When a program misbehaves, print where each machine is. `sim.chip.mach[m].state` is the entry machine m will
run next, numbered as in the listing:

```python
sim = Sim(TableChip(), result.image)
sim.send(0, b"\x41")
last = None
for _ in range(1200):
    sim.tick()
    state = sim.chip.mach[0].state
    if state != last:
        print(sim.cycle, state, result.listing[state].split()[1])
        last = state
```

It prints the clock and each new state: `1 0 tx.idle`, `17 1 tx.line12`, `18 12 tx.line13`,
`19 2 tx.line13.hold`, `539 3 tx.bit`, and so on. `sim.chip.mach[0]` also has `T`, `N`, `K`, `J`, `S`, `R` and
`crc`.

## 9. Load it into the chip

Write the image to a file:

```
python3 -c "import sys; from bakeoff.cand_table.compiler import compile_file; sys.stdout.buffer.write(compile_file(sys.argv[1]).image)" docs/examples/uart_8e1_tx.fsm > uart_8e1_tx.bin
```

`uart_8e1_tx.bin` is 123 bytes, starting `01 00 00 00 00 01 00 00 00`: one machine, its pin map, and the
polynomial 1. The host resets the chip, writes the file to port 2 a nibble at a time, and waits for RUNNING.
[isa.md, "Load into the chip"](isa.md#73-load-into-the-chip) lists the steps, and [info.md](info.md#the-host-link)
the pin timing.

## 10. Variations

Each of these was run through the same checks.

1. **Odd parity:** `TX=!c` in the transmitter's parity line, and `match=1` in the receiver. The script checks
   even parity, so its `parity()` must return the inverse too (`... & 1 ^ 1`), or it fails both programs.
2. **Two stop bits:** `TX=1, hold 2*BIT => idle` in the transmitter. The receiver accepts them unchanged: it
   checks the first.
3. **Another baud rate:** change `BIT`. At 60 MHz a bit must be at least a few clocks; the bench program
   [uart_3m.fsm](../bakeoff/cand_table/programs/uart_3m.fsm) runs at 3 Mbaud, 20 clocks a bit.

## 11. Patterns from the bench programs

The 14 bench programs are in [bakeoff/cand_table/programs/](../bakeoff/cand_table/programs/), and each one passes
its bench in [bakeoff/harness/benches/](../bakeoff/harness/benches/). These three patterns cover most of what a
new protocol needs.

### 11.1 Bit loops with the shifter and a counter

A bit loop drives or samples one bit, shifts S, counts down K, and leaves when K reaches 0. The simplest is the
transmitter above, where `hold` makes each bit one line. When the protocol needs every bit exactly the same
length, including the bit after a byte boundary, count clocks on every path round the loop and do the work
between bytes inside the clocks a bit spends waiting.
[spi_ctrl_m0.fsm](../bakeoff/cand_table/programs/spi_ctrl_m0.fsm) keeps a 12-clock SCK period across bytes:

```
bit:    SCK=0, MOSI=d, hold 6        # clocks 0-5: falling edge, data out
        SCK=1, hold 3                # clocks 6-8: rising edge
        shift MISO, K-=1 => kz: byte # clock 9: MISO at that edge. The rule sees K as it was: 7 down to 0
        hold 2 => bit                # clocks 10-11
byte:   push1=S => nz: last | rx0: load # clock 10: the byte read, and is the next one here?
        wait rx0                     # it is not: the clock stops
load:   S=pop0, K=7, N-=1 => bit     # clock 11: the next byte to send
```

Both ways from clock 10 back to `bit` take 2 clocks: `hold 2`, or `byte` and `load`. Only a host that falls
behind adds to them, and SCK stands still meanwhile. The comments count clocks; keep doing that. Two things to
copy from it:

1. **Allow for the pads when you sample.** A write reaches the pin a clock later and a read sees the pins of 2
   clocks before, so a `shift MISO` on the line that makes an edge reads MISO 3 clocks before that edge. Here it
   comes 3 clocks after it.
2. **Test `rx0` before every pop.** A pop from an empty channel gives 0 and raises no flag.

Two more ways to keep time:

1. **Run the timer in the background.** T counts down in every state, so load it when a bit starts, do the
   bookkeeping, and `wait tz`. [ws2812_tx.fsm](../bakeoff/cand_table/programs/ws2812_tx.fsm) fetches bytes and
   counts pixels while each 75-clock bit runs out.
2. **One line per clock, with every loop path the same length.** At 6 clocks per bit,
   [eth10_tx.fsm](../bakeoff/cand_table/programs/eth10_tx.fsm) writes each bit as six lines and makes every path
   round every loop exactly six lines, so no half bit is stretched.

K holds 15 at most and S 8 bits: a longer word is bytes counted by J or N, as in
[onewire.fsm](../bakeoff/cand_table/programs/onewire.fsm), which reads 8 bytes with J.

### 11.2 Edges, time-outs and clock recovery

`wait => edge: X | tz: Y` waits for input A to change, and gives up when the timer runs out. That one line is a
time-out, an end-of-frame detector, and, when every edge reloads the timer, clock recovery: the receiver re-times
itself on the sender's edges. [eth10_rx.fsm](../bakeoff/cand_table/programs/eth10_rx.fsm), after the
start-of-frame byte:

```
frame:  K=8, N=0, crc=init
        T=9
idle:   wait => edge: bit | tz: end  # no edge for two bit times: the frame is over
bit:    shift RX, crc+=RX, K-=1, T=9 # edge + 1
        => kz: byte                  # edge + 2
        nop
        nop => idle                  # edge + 4
byte:   push1=S, N+=1, K=8           # edge + 3
        nop => idle
```

In Manchester code every bit has an edge in its middle. The receiver takes the bit one clock after that edge,
then looks away for three clocks, past any edge between two bits, and waits for the next middle edge. Each edge
reloads T, so the frame ends when the line stops changing.
[usb_ls_rx.fsm](../bakeoff/cand_table/programs/usb_ls_rx.fsm) does the same for NRZI: `wait => edge: data0 | tz:
data1`, where an edge is a 0, a whole cell with no edge is a 1, and every edge restarts the timer. `edge` is seen
only on a line that tests it, so the work of a bit must end before the soonest next edge can come: there the line
that tests the CRC after a byte tests `edge` as well.

Other forms of the same idea:

1. **A plain time-out on a level:** [onewire.fsm](../bakeoff/cand_table/programs/onewire.fsm) releases the bus
   and, 10 us later, finds a short if it is still low: `DQ=z, hold 10*US`, `=> !DQI: absent`. 60 us after that it
   tests whether a device pulled it low: `hold 60*US`, `=> DQI: absent`.
2. **Waiting for the other side, but not for ever:** [i2c_ctrl.fsm](../bakeoff/cand_table/programs/i2c_ctrl.fsm)
   releases SCL and takes `wait => SCLI: high | tz: tick`, so a target that stretches the clock is waited for.
   T alone counts to 1.09 ms. The program makes that 25 ms with a spare LFSR as a round counter:
   `tick: crc>>=1, T=65535 => crc: stuck | cwait` gives up once the LFSR's 1s have all been shifted out.
3. **Settle before `edge`.** `edge` compares input A with its level on the previous turn, and after reset that
   level counts as 1. eth10_rx starts with `hold 4` so no false edge is taken.

Only input A has `edge`, so declare the pin you time on first.

### 11.3 CRC and parity

The LFSR does both. Configure it with a bit-reversed polynomial, feed it every bit, and either send it or
compare it.

| Program | `crc` line | What it computes |
|---|---|---|
| [ps2_rx.fsm](../bakeoff/cand_table/programs/ps2_rx.fsm) | `poly=1 match=1` | Odd parity over 8 data bits and the parity bit |
| [onewire.fsm](../bakeoff/cand_table/programs/onewire.fsm) | `poly=0x8C match=0` | CRC-8, x^8 + x^5 + x^4 + 1; a good ROM code leaves 0 |
| [usb_ls_tx.fsm](../bakeoff/cand_table/programs/usb_ls_tx.fsm) | `poly=0xA001 init=0xFFFF` | CRC-16, x^16 + x^15 + x^2 + 1, sent inverted |
| [usb_ls_rx.fsm](../bakeoff/cand_table/programs/usb_ls_rx.fsm) | `poly=0xA001 init=0xFFFF match=0xB001` | The same, checked by its residue over data and CRC |
| [eth10_tx.fsm](../bakeoff/cand_table/programs/eth10_tx.fsm) | `poly=0xEDB88320 init=0xFFFFFFFF` | CRC-32, sent inverted |
| [eth10_rx.fsm](../bakeoff/cand_table/programs/eth10_rx.fsm) | `poly=0xEDB88320 init=0xFFFFFFFF match=0xDEBB20E3` | CRC-32, checked by its residue |

1. **Feed it.** A transmitter steps with the bit it sends, in the same line: `TX=d, shift, crc+=d`. A receiver
   steps with the input it shifts in: `shift RX, crc+=RX`. Clear or load it first with `crc=0` or `crc=init`.
2. **Send it.** Drive the pin with LFSR bit 0 and shift the LFSR right, one bit per bit time. eth10_tx sends the
   CRC inverted, low bit first, with `TXP=c, TXN=!c` and then `TXP=!c, TXN=c, crc>>=1` three clocks later.
3. **Check it.** Step the received CRC bits through the LFSR too, then test `crc`, which is true when the LFSR
   equals `match`. For a CRC sent inverted, `match` is the CRC's residue, not 0. Leave one line between the last
   step and the test.
4. **A CRC defined most significant bit first** works the same way when its polynomial, start and match values
   are all written bit-reversed and its bits are fed in the order the definition takes them.

### 11.4 More, in one line each

1. **A request with a length:** `N=pop0`, then a loop that ends on `nz`
   ([spi_ctrl_m0.fsm](../bakeoff/cand_table/programs/spi_ctrl_m0.fsm)). A 16-bit length is `N=pop0`, then
   `Nhi=pop0` ([eth10_tx.fsm](../bakeoff/cand_table/programs/eth10_tx.fsm)).
2. **Open drain:** `X=0` and `X=z`, or `X=od` to send S's bits, with an `in` on the same pin to read the bus
   ([i2c_ctrl.fsm](../bakeoff/cand_table/programs/i2c_ctrl.fsm), [onewire.fsm](../bakeoff/cand_table/programs/onewire.fsm)).
3. **Shared code:** a macro for code that differs per use, a `call` for code that is the same
   ([i2c_ctrl.fsm](../bakeoff/cand_table/programs/i2c_ctrl.fsm) has both).
4. **NRZI and bit stuffing:** `toggle` for a 0, J counting 1s, a stuffed bit after six
   ([usb_ls_tx.fsm](../bakeoff/cand_table/programs/usb_ls_tx.fsm)).
5. **Finding a byte in a stream:** shift every bit in and test `S==0xD5` on the next line
   ([eth10_rx.fsm](../bakeoff/cand_table/programs/eth10_rx.fsm)).
6. **Answering at the pads' floor:** `MISO=z when CS` releases a pin in the clock that sees CS rise
   ([spi_target_m0.fsm](../bakeoff/cand_table/programs/spi_target_m0.fsm)).
7. **Reporting to the host:** good bytes on channel 1, status bytes on channel 2. Test `tx1` before a push if
   the host might fall 4 bytes behind.

## 12. Before you call a program done

1. It compiles with no warning that a state is never reached.
2. Every count of clocks is written in a comment, and every path round a timed loop adds up.
3. No line tests a counter it changes itself, unless it is a `hold` line.
4. There is a line between the last LFSR step and every `crc` test.
5. Input sample points allow for the 2-clock synchroniser.
6. With several machines, every count is in turns.
7. A script runs it on the model against a decoder written from the spec, at the far end's slowest and fastest
   clock, and the script fails when the program is broken on purpose.

## 13. Using RTL v2 features in new protocols

1. **Opcode & bit-field dispatch in 1 clock:** Replace multi-line `S==...` compare chains with top-level `tcam VAL/MASK -> CLASS` rules and `dispatch tcam4` / `tcam8` / `tcam16` or `dispatch S[7:4]` (see [usb_ls_rx.fsm](../bakeoff/cand_table/programs/usb_ls_rx.fsm), [learn/host/clone_spi.py](../learn/host/clone_spi.py), and [examples/qubx_risk_gate/risk_gate.fsm](../examples/qubx_risk_gate/risk_gate.fsm)).
2. **Hardware `nRESET` / Kill-Switch on a 4th input:** Bind a 4th pin in `in` (e.g. `in SCK=P0, MOSI=P1, CS=P3, NRST=P4`) when a machine does not need `rx3`; check `!NRST` on critical branches to react within 1 turn.
3. **Sub-clock edge tracking (8.33 ns):** Use `P2.half` and `edge_b` when recovering jittery 10 Mbit/s line clocks (see the 3-point deadband DPLL in [eth10_rx.fsm](../bakeoff/cand_table/programs/eth10_rx.fsm)).
4. **On-chip logic analyzer:** Arm Port 3 (`0x03`, `mask`, `0x02`) before triggering a transaction, then stop (`0x00`) and drain `{pins, delta}` pairs to measure exact cycle-accurate wire-to-wire timing without an external logic analyzer.

