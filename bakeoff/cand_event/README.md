# cand_event: an event machine with a bank of generic helpers

Instructions do not count cycles. Each one waits for an event and then acts in a single cycle. The work that
is too fast for instructions (10 Mbit Manchester has 3-cycle half bits) is done by six generic helpers that the
program configures; the program keeps the policy: framing, lengths, errors, what a packet's end means.

```bash
python3 -m bakeoff.run --candidate cand_event     # 14 of 14 pass
python3 -m bakeoff.cand_event.selfcheck           # two lanes at once, timing, limits, receive sweeps, budget
```

## The machine

1. There are **two identical lanes**. A lane is one engine, its helpers and its own 512×32 program SRAM.
2. The lanes share the eight pins, the four host FIFOs (4 bytes deep) and two flags (`sig0`, `sig1`).
3. An instruction is `when EVENT: action`. It waits until the event is true **and** its operands are ready
   (a FIFO has a byte or room, the CRC unit is idle, the shifter has a word), then does everything in one cycle.
4. Timing a programmer needs, with T the cycle an instruction fires:
   1. A `pin` change is on the pad at T+1. The pins a program sees are 2 cycles old.
   2. The next instruction can fire at T+2 (one cycle is the SRAM read), or T+3 after `djnz`.
   3. `#N -> t1` at T makes `when t1` fire at exactly T+N (N ≥ 2). `act` strobes go through a register: the
      helper acts at T+1, so a pin it moves changes on the pad at T+2.
   4. A timer or helper keeps its own time, so program latency never accumulates into the waveform.

## Instructions (one 32-bit word each: opcode 4, event 6, payload 22)

| Form | Effect |
|---|---|
| `[when EV:] SRC -> DST[, DST2]` | Move 16 bits. `SRC.hi` takes the high byte; `rN.hi` as DST writes only the high byte. |
| `[when EV:] #N -> DST` | Load a constant. |
| `[when EV:] add\|sub\|and\|or\|xor\|cmp\|tst rD, rS\|#N`, `shl rD`, `shr rD` | 16-bit ALU on r0..r3; sets `z` and `c`. |
| `[when EV:] act STROBE, ...` | Pulse helper commands. A bare `when EV` just waits. |
| `[when EV:] pin p0=0, p3=1, p5=z` | Drive low, drive high or release any of the eight pins at once. |
| `br EV, L` | Branch if the event is true now (never waits). |
| `[when EV:] goto L` / `call L` / `ret` | Jump; one-level subroutine. |
| `[when EV:] djnz rN, L` | Decrement and loop. |
| `trap EV -> L` / `trap off` / `resume` | Arm a one-shot escape: when EV comes, whatever the program is waiting on, jump to L. `resume` returns. |
| `halt` | Stop this lane's engine. |
| `cfg GROUP field=value ...` | Assembler shorthand for a constant moved into a configuration register. |

1. **Events:** `p0..p7` (pin high), `t0`, `t1`, `rx.full`, `tx.ready`, `tx.done`, `match`, `crc.busy`, `in0`, `in3`,
   `out1`, `out2`, `z`, `c`, `smp.edge`, `smp.run`, `rl.err`, `sig0`, `sig1`. A `!` inverts any of them.
2. **Sources:** `r0..r3`, `fifo0`, `fifo3`, `rx`, `crc.lo`, `crc.hi`, `pins`, `t0`, `t1` (a timer's count).
3. **Destinations:** `r0..r3`, `fifo1`, `fifo2`, `tx`, `crc` (feed a byte), `crc.lo`, `crc.hi`, `t0`, `t1` (set and
   start), and 15 configuration registers. Configuration is an ordinary destination, so a baud rate can come
   from the host at run time.
4. **Strobes:** `tx.tick`, `tx.flush`, `rx.shift`, `rx.clear`, `smp.arm`, `smp.stop`, `rl.clear`, `t0.stop`, `t1.stop`,
   `sig0.set`, `sig0.clr`, `sig1.set`, `sig1.clr`.

## Helpers (per lane; none knows a protocol)

| Helper | What it does | Scenarios that use it |
|---|---|---|
| Timers `t0`, `t1` | 16-bit, one-shot or repeating. A timer can restart on any event (a watchdog). `t0` is also the transmitter's bit clock. | 11 (not uart_rx, spi_target, ps2) |
| Sampler | Says *when* to sample: tracks edges on a clock pin and ticks `phase` cycles later; samples a data pin. | uart_rx, uart_3m, spi_target, ps2, usb_rx, eth_rx |
| Receive shifter | 1 to 16 bits, either order, with a capture register. Decodes NRZ, inverted or NRZI. Clocked by the sampler, by the transmitter (mid-bit or end of bit) or by `act rx.shift`. | 10 (all that receive) |
| Transmit shifter and coder | 1 to 16 bits with a holding register. Codes NRZ, NRZI or Manchester; `split` gives two ticks per bit, and a pin can carry the bit clock. | uart, spi, i2c, usb_tx, eth_tx |
| Run-length unit | Counts equal 1s; after N it inserts a 0 (send) or deletes a bit (receive). | usb_tx, usb_rx |
| CRC unit | Any polynomial up to 32 bits, either bit order, optional inverted output; one bit per cycle, a byte per feed. With `x+1` it is a parity unit. | onewire, ps2, usb_tx, usb_rx, eth_tx, eth_rx |
| Pattern matcher | Masked 16-bit compare on the live receive bits or on the pins. | eth_rx (delimiter), usb_rx (SE0) |

1. **Sampler modes.** `once`: wait for an edge, then tick every `period` until re-armed (UART). `free`: every edge
   re-centres a running bit clock (USB at ±1.5%). `gate`: one tick per edge, and edges are ignored until that tick
   (Manchester, and any externally clocked bus: SPI target, PS/2).
2. **Pins.** A pin follows `tx`, `~tx` or `clk` while the transmitter is busy and its own register otherwise, so a
   program presets what the line falls back to (SE0, TP_IDL, hi-Z). `cfg od` makes a pin open drain in hardware.
3. **How they connect** is fixed and small: sampler → decode → destuff → receive shifter → matcher; holding →
   transmit shifter → stuff → coder → pins. Everything else (which bytes the CRC covers, where a frame ends, what
   to report) is a move in the program.

## A whole program: 10 Mbit Ethernet receive (the loop of `programs/eth10_rx.s`)

```
frame:  when !p2                    ; the line is idle
        act smp.arm, rx.clear
        #0xFFFF -> crc.lo           ; (and crc.hi, and #0 -> r1)
        when match: act rx.clear    ; the start delimiter: bytes are counted from here
        #12 -> t1                   ; t1 restarts on every edge, so it expires when the carrier stops
        trap t1 -> eof
byte:   rx -> fifo1, crc            ; one byte to the host and to the CRC unit
        add r1, #1
        goto byte
eof:    crc.lo -> r0                ; then compare with the residue, push status and r1, goto frame
```

## How the honesty rules are met

1. **Logic depth.** The longest paths are: a 32:1 event mux, then the fire signal, then a write enable or a pad
   register; a 12:1 source mux, a byte-lane mux, then a register; a timer's 16-bit compare into a flag. The ALU
   (4:1 operand mux, 16-bit add, result mux) is one mux over the rule, so it is a two-cycle path: it shares its
   second cycle with the SRAM read of the next instruction, and `djnz`, whose branch needs the result, costs 3.
   `tx.done` in the cycle of the last tick comes from a flop set one cycle ahead (counted in the budget).
2. **SRAM.** One read port per lane. Every instruction pays one read cycle, and it is modelled.
3. **CRC.** One bit per cycle: a byte keeps the unit busy for 8 cycles and a move to or from it waits.
4. **No protocol names.** UART framing is a 10-bit word built with `shl` and `or`; USB's SYNC is the constant `0x80`.
5. **`load()`** copies words into the two SRAMs and does nothing else. All configuration is instructions.

## Budget

| Per lane | Flops |
|---|---|
| Engine: pc 9, link 9, trap link 9, trap 16, flags 2, state 3 | 48 |
| Registers r0..r3 | 64 |
| Timers t0, t1: count 16, period 16, state 4, retrigger 6, each | 84 |
| Sampler: count 16, period 16, phase 16, config 12, state 5 | 65 |
| Receive shifter: live 16, capture 16, count 5, flags 2, config 8 | 47 |
| Transmit shifter and coder: shift 16, holding 16, count 5, state 6, config 13 | 56 |
| Run-length unit: limit 4, count 4, error 1 | 9 |
| CRC unit: register 32, polynomial 32, byte 8, count 4, config 2 | 78 |
| Pattern matcher: value 16, mask 16, source 1, flag 1 | 34 |
| Strobe register 13; pin routing (source 16, open drain 8) 24 | 37 |
| **One lane** | **522** |

| Shared | Flops |
|---|---|
| Pad registers: out 8, oe 8, input synchronisers 16 | 32 |
| Host FIFOs: 4 × (4 bytes + 5 pointer bits) | 148 |
| Host link and program loader | 70 |
| Cross-lane flags | 2 |
| **Whole chip: 2 × 522 + 252** | **1,296** of about 3,000 |

1. **SRAM:** 32,768 bits, two 512×32 macros, one per lane. The largest program is 78 words (i2c_ctrl).
2. **Gates:** about 4,800 combinational cells (2 × 2,015 per lane + 750 shared), itemised in `machine.py`. With
   the flops that is about 6,100 of the 32,000 cells. This is an estimate from mux and adder widths, not synthesis.

## How many protocols run at once

Two, one per lane, and any two: a lane has every helper, so both can be fast ones (USB receive beside Ethernet
send). A full-duplex protocol takes one lane: `uart_3m` sends from the main loop and receives through the trap.
`selfcheck.py` runs a 3 Mbaud full-duplex UART on lane 0 while lane 1 runs a 5 MHz SPI controller.
The limits are one receive and one transmit pipeline per lane, and two host channels in each direction.

## Tiny Tapeout's 24 pins

1. **`uio[7:0]`** are P0..P7, the protocol pins.
2. **`ui_in`:** `[3:0]` nibble in, `[5:4]` channel, `[6]` strobe (toggles per nibble), `[7]` load mode.
3. **`uo_out`:** `[3:0]` nibble out, `[7:4]` flags: channels 1 and 2 have a byte, channels 0 and 3 have room.
4. With the strobe through a two-flop synchroniser the link moves a byte in about 8 cycles. That meets the
   harness's 16 cycles per channel while two channels are busy; with all four busy each gets a byte per 32.
   No scenario needs more than a byte per 48 cycles in a direction.
5. In load mode the nibbles go to the loader: 8 per word, into the lane the channel bits name, engines held in
   reset. The chip keeps nothing when deselected, so the host reloads (at most 2 × 512 words).

## What it does that a PIO cannot

1. It recovers a bit clock from edges, in hardware: 10 Mbit Manchester receive and USB at ±1.5%.
2. It computes any CRC up to 32 bits on chip (USB CRC-16, Ethernet FCS, 1-Wire CRC-8, parity).
3. It stuffs and destuffs bits, and finds a sync pattern in a bit stream, with no instructions spent.
4. A wait can be escaped: `trap` gives every wait a timeout or an abort. A PIO `wait` blocks forever.
5. It has 16-bit timers: a 500 µs delay is one instruction, and the timer's count can be read as a pulse width.
6. It has a 16-bit ALU and four registers, so lengths, counts, status bytes and address bytes are made on chip.

## Limits found while building it

1. **A program that answers every external clock edge is slow.** `spi_target_m0` shifts MISO from the program.
   It passes at 2 MHz; `selfcheck.py` finds it still passes at 4.3 MHz (14 cycles per SCK period) and fails at
   4.6 MHz (13). The fix is one configuration bit (clock the transmit shifter from the sampler's other edge) and
   it is not built.
2. **Ethernet receive has one valid sample position.** `phase` must be exactly 4: 3 and 5 both fail the bench.
   At 60 MHz a 50 ns half bit is 3 samples, so real line jitter would need a faster or two-phase sampler.
3. **The configuration surface is wide.** There are 15 registers with bit fields, and a wrong field fails silently.
   `eth10_rx` needs 9 configuration lines before its first wait.
4. **One of each helper per lane.** The run-length unit serves send or receive, not both; a protocol that needs a
   CRC in both directions at once needs both lanes.
5. **`trap` is one-shot and does not nest,** and `call` has one level. A handler that interrupts a
   multi-instruction sequence must leave its registers alone.
6. **No framing checks in the short programs.** `uart_rx` does not test the stop bit; `usb_ls_rx` does not test SYNC.

## Scenarios that fail

None. All 14 pass. `selfcheck.py` also runs both receivers beyond the benches, with the benches' own signal
generators: `eth10_rx` over 24 clock phases, ±200 ppm and three frame sizes (360 runs), and `usb_ls_rx` over 16
phases, ±1.5% and five payloads (800 runs). None fails, and a flipped bit and a bad CRC are both reported.

## Files

1. `isa.py` holds the encodings; `machine.py` the simulator and budget tables; `asm.py` the assembler.
2. `programs/<scenario>.s` is one source per scenario (7 to 78 instructions); `demo_two_lanes.s` is the self-check's.
