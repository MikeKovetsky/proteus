# cand_pio: PIO-style state machines with a metronome

All 14 scenarios pass (`python3 -m bakeoff.run --candidate cand_pio`). The chip is four small state machines in
the spirit of the RP2040 PIO, 1,134 flops and two 512×32 program memories. Nothing fails; the limits are listed
at the end.

The one idea that is not in a PIO: the clock divider does not slow the machine down. It is a metronome. Every
instruction runs at the full clock, and any instruction can carry `@tick`, which holds it until the machine's
timer ticks. Line changes are pinned to ticks, so the code between two ticks can take any path (bit stuffing, a
byte boundary, loading a CRC) and the edges still land on the grid.

## The machine

1. **Four state machines.** Each has X and Y (16 bits), an input shift register ISR (16 bits), an output shift
   register OSR (32 bits), a program counter, one link register for `call`/`ret`, an 8-bit delay counter and a
   16.8 fractional timer.
2. **Two program memories,** A and B, 512 words of 32 bits each, synchronous, one port. Machines 0 and 1 run
   from A, machines 2 and 3 from B. A machine alone on its memory runs one instruction per 60 MHz clock. Two
   machines on one memory take alternate clocks, 30 MHz each.
3. **Sixteen pins.** `p0`–`p7` are the pads. `v0`–`v7` are internal flops that every instruction treats as pins:
   they are 1-bit variables (`out v0, 1` then `jpin`) and the signals between machines. A pad can be marked
   open drain: writing 1 releases it, writing 0 pulls it low, and it is never driven high.
4. **Four byte FIFOs,** 4 deep, one per host channel. `pull` takes from channel 0 or 3, `push` writes to 1 or 2.
5. **One CRC unit,** shared: a 32-bit register shifted right, with a programmable polynomial and a programmable
   residue to compare against. It taps the bits one machine shifts in (or out), one bit per clock.
6. **The timer** of each machine counts clocks and raises a tick every `period` clocks (16 bits and 8 fractional
   bits, so 520.83 works). `timer restart` and `timer half` set its phase.

## Instruction set

A word is `op[31:28] T[27] side_en[26] side[25:24] delay[23:16] arg[15:0]`. Every instruction may carry a
side-set (one or two pins, written on the instruction's first clock), a delay of 0 to 255 clocks after it, and
`@tick`. Pins are named in the instruction, so there are no pin-mapping registers.

| Instruction | What it does |
|---|---|
| `jmp [cond,] label` | Conditions: `!x`, `x--`, `!y`, `y--`, `x!=y`, `!osre`, `!isre`, `crcok`, `!crcok`. |
| `call label`, `ret` | One level of subroutine. |
| `jpin level, pin, label` | Jump if the pin is at that level. |
| `wait level, pin` | Stall until the pin is at that level. |
| `waitt level, pin, n, label` | The same, but after `n` more clocks (or on the `tick`) jump to `label` instead. |
| `in src, n` | Shift `n` bits into ISR from a pin, `x`, `y`, `null`, `ones` or `osr`. |
| `out dst, n` | Shift `n` bits out of OSR to a pin, `x`, `y` or `null` (`outdir` for pad directions). |
| `push [ch2] [, x / y / #imm]` | A byte to channel 1 or 2: the ISR byte by default, or X, Y or a constant. |
| `pull [ch3] [noblock]` | A byte from channel 0 or 3 into OSR. |
| `mov dst, [~]src [, n]` | Between `x`, `y`, `isr`, `osr`; sources also `null`, `pins`, `crc`. |
| `set x / y, imm13` | Load a constant. |
| `set`, `dir`, `toggle pin[:n], v` | Write, set the direction of, or invert up to 8 pins. |
| `crcinit v`, `timer restart / half`, `nop`, `halt` | Start a CRC; set the timer's phase. |

Directives: `.sm n`, `.tick period`, `.shift in=left|right out=left|right`, `.autopush [ch2]`,
`.autopull [ch3]`, `.side pin[:2] [dirs]`, `.pads out= oe= od=`, `.crc poly= residue= tap=in|out`, `.define`.
`python3 -m bakeoff.cand_pio.asm programs/usb_ls_tx.pio` prints a listing. The image is the configuration
writes (5 bytes each) followed by the words of memory A and B, as the loader would write them after reset.

## How it keeps the honesty rules

1. **Synchronous memory.** The simulator keeps each memory's latched address and executes only the word read
   from it (`rd_addr` in [chip.py](chip.py)). A machine alone on its memory puts its *next* address on the
   address pins in the same clock it executes, so a taken jump costs nothing. With two machines on a memory the
   address comes from the other machine's PC register.
2. **Logic depth.** Nothing multiplies or divides. The deepest paths are:
   1. next address: memory output → decode → one condition (a 16:1 pin mux or a stored zero flag) → 4:1 address
      mux → memory address;
   2. `in` from pins: a 16:1 pin mux, then a 16-bit shifter;
   3. `out`: a 32-bit shifter, then the pin write decode.
   None holds an adder. Path 1 includes the memory's access and set-up times, and it is the timing risk: it has
   not been synthesised.
3. **CRC.** One bit per clock, only from 1-bit shifts. `crcok` is a registered compare, valid two clocks after
   the last bit.
4. **No protocol names.** The helpers are the timer, the CRC unit, the internal pins, `waitt`, `toggle` and
   `call`. Each is used by at least two scenarios.

## Budget

| Per machine | Flops | Shared | Flops |
|---|---|---|---|
| PC and link register | 18 | Pad out and enable registers | 16 |
| X and Y | 32 | Open-drain mask | 8 |
| ISR and bit count | 21 | Internal pins | 8 |
| OSR and bit count | 38 | Pad input synchronisers | 16 |
| Delay / time-out counter | 8 | CRC register, polynomial, residue, tap, flag | 101 |
| Run state | 3 | Four 4-byte FIFOs with pointers | 156 |
| Timer period and counter | 48 | Memory slot phase | 1 |
| Tick pending, settings | 14 | Loader and host link (estimate) | 100 |
| **Total per machine** | **182** | **Total shared** | **406** |

1. **Flops: 1,134** (4 × 182 + 406), 38% of the 3,000 budget.
2. **SRAM: 32,768 bits** (two 512×32 macros). The largest program here is 85 words. A one-memory variant
   (16 kbit, two machines) runs every scenario in this bake-off.
3. **Gates: about 7,000** combinational cells (1,500 per machine, 900 shared). This is an estimate from the
   shifters and muxes, not a synthesis result.

## Concurrency

1. Four protocols run at once at 30 MHz each, or two at 60 MHz (one per memory), or one at 60 MHz and two at
   30 MHz.
2. `eth10_tx`, `eth10_rx` and `usb_ls_rx` need a 60 MHz machine. The other eleven pass at 30 MHz.
3. The host has two channels each way and there is one CRC unit, so two CRC protocols cannot run together.
4. `python3 -m bakeoff.cand_pio.checks` shows both: every program re-run with a second machine enabled on its
   memory, and four machines at once (a 115200 baud loop on memory A, a 3 Mbaud loop on memory B).

## Pins on Tiny Tapeout

1. `uio[7:0]` are P0–P7, the eight protocol pads.
2. `ui[3:0]` carry a nibble from the host, `ui[4]` picks channel 0 or 3, and `ui[5]` is a strobe that toggles
   per nibble. `ui[6]` picks channel 1 or 2 to read and `ui[7]` is the read strobe.
3. `uo[3:0]` carry a nibble to the host. `uo[4]` and `uo[5]` say channels 0 and 3 have room, `uo[6]` and `uo[7]`
   say channels 1 and 2 hold data.
4. A nibble takes 4 clocks through the synchroniser, so the link moves two bytes each way in 16 clocks: the
   contract's rate.
5. After `rst_n` the first bytes written are the image. Its header gives the lengths, and the machines start
   when the last word is in. A deselected chip is reloaded the same way.
6. The link is a proposal. The harness models the FIFOs directly, and the 100 flops for it are an estimate.

## What it does that a plain PIO cannot

1. **`@tick`.** In a PIO the divider slows every instruction, so timing resolution and reaction time trade
   off, and every path through a loop has to be padded to the same length. Here `usb_ls_tx` takes four
   different paths between bits and no delay is counted by hand.
2. **CRC on chip.** One unit does the CRC-32 of Ethernet, the CRC-16 of USB, the CRC-8 of 1-Wire and, with the
   polynomial set to 1, the parity bit of PS/2. A PIO leaves all four to the CPU.
3. **Wait with a time-out.** `waitt` gives USB its clock recovery (an edge before the tick is a 0, a tick with
   no edge is a 1), Ethernet its end of frame, and the SPI target its check of CS.
4. **512-word programs** against the PIO's 32, so I2C with its NACK path fits (85 words), with `call`/`ret`
   for "send a byte".
5. **Internal pins.** A bit shifted out of OSR can be branched on without spending X or Y.
6. **Open-drain pads,** so I2C and 1-Wire programs write the line level and cannot drive it high.

It gives up, against the RP2040: 32-bit X, Y and ISR, `wrap`, `out exec`, bit reversal, and interrupts to a CPU.

## Programs

| Scenario | Words | Bytes | How |
|---|---|---|---|
| `uart_tx` | 6 | 44 | One tick per bit. |
| `uart_rx` | 8 | 47 | The start edge sets the timer half a bit out of phase. |
| `uart_3m` | 14 | 86 | Two machines on memory A, 30 MHz each. |
| `spi_ctrl_m0`, `_m3` | 13, 12 | 72, 68 | One tick per half period; the same loop for both modes. |
| `spi_target_m0` | 18 | 82 | Follows SCK; looks at CS every other clock. |
| `i2c_ctrl` | 85 | 360 | Waits out clock stretching, flushes the message after a NACK. |
| `ws2812_tx` | 19 | 96 | Three ticks per bit. |
| `ps2_rx` | 22 | 113 | Parity from the CRC unit. |
| `onewire` | 41 | 194 | 5 µs ticks, CRC-8 by residue. |
| `usb_ls_tx` | 38 | 182 | NRZI, stuffing and CRC-16 in the program and the CRC unit. |
| `usb_ls_rx` | 50 | 230 | Clock recovery, unstuffing, CRC-16 by residue. |
| `eth10_tx` | 44 | 206 | Six instructions per bit, all six clocks used. |
| `eth10_rx` | 36 | 169 | Waits for each mid-bit edge; the bit is read one clock later. |

## Failing scenarios

None. These are the limits behind the passes:

1. **Ethernet has no slack.** A bit is six clocks and both loops use all six, so the machine must be alone on
   its memory, and the transmit loop is written four times (1 or 0, data or FCS) to make every path fit.
   Receive samples one clock after the edge and four clocks after it. That is right for every phase and
   ±200 ppm (360 runs in `checks.py`), but a real line with jitter has less than one clock of margin.
2. **The 60 MHz claim rests on one unsynthesised path** (next address, above). If it does not close, a jump
   costs two clocks and both Ethernet loops break. The other twelve scenarios would survive that.
3. **Two scratch registers.** USB and I2C keep state in internal pins and in ISR, and lengths are counted down
   from 0xFFFF and inverted, because there is no adder.
4. **`usb_ls_rx` is too long for 30 MHz.** A 0 bit costs 19 instructions between edges, and at two clocks each
   that is 38 of the 39.4 clocks a fast sender leaves. At 60 MHz it passes 800 runs over phase and ±1.5%.
5. **A wait watches one pin.** The SPI target polls CS between looks at SCK, and lets go of MISO about five
   clocks after CS rises.
6. **Not tested here:** the host link and loader, protocols outside the 14 scenarios, and a target-role I2C.
