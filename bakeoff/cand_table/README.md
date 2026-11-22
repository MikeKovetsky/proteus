# cand_table: a state table in SRAM, compiled from a state-machine language

All 14 scenarios pass, and the programs still have known bugs. The chip is one 512-entry table of 64-bit states and a small generic datapath. A
machine reads one entry per clock, branches four ways on two inputs, and runs several operations
in that same clock. It was budgeted at 1,202 flops (the RTL has 1,172) and both SRAM macros. Programs are large: 107 to 827 bytes.

```bash
python3 -m bakeoff.run --candidate cand_table
```

```bash
python3 -m bakeoff.cand_table.selfcheck
```

```bash
python3 -m bakeoff.cand_table.compiler bakeoff/cand_table/programs/eth10_rx.fsm
```

The first prints the table below. The second checks four protocols at once, the pin reaction time, a reload, and
that the compiler only does arithmetic. The third prints a program's listing: every entry, its state and successors.

| Scenario | Result | Source lines | States | Table entries | Image bytes |
|---|---|---|---|---|---|
| uart_tx | pass | 9 | 8 | 11 | 107 |
| uart_rx | pass | 14 | 13 | 17 | 155 |
| uart_3m (two machines) | pass | 22 | 21 | 29 | 267 |
| spi_ctrl_m0 | pass | 20 | 22 | 28 | 243 |
| spi_ctrl_m3 | pass | 19 | 21 | 27 | 235 |
| spi_target_m0 | pass | 23 | 19 | 34 | 291 |
| i2c_ctrl | pass | 72 | 92 | 101 | 827 |
| ws2812_tx | pass | 25 | 23 | 31 | 267 |
| ps2_rx | pass | 25 | 20 | 30 | 259 |
| onewire | pass | 33 | 37 | 41 | 347 |
| usb_ls_tx | pass | 47 | 65 | 84 | 691 |
| usb_ls_rx | pass | 51 | 43 | 82 | 675 |
| eth10_tx | pass | 57 | 57 | 64 | 531 |
| eth10_rx | pass | 24 | 21 | 23 | 203 |

## The machine

1. **One state is one table entry.** Word A of the entry sits in the first 512x32 macro and word B in the
   second, at the same address, so both macros are read together.
2. **The next state is two wires.** An entry names a `base` address and two conditions. The next address is
   `base | cA | cB << 1`. The compiler lays each state's two or four successors side by side, and copies a
   state into every block that needs it. No adder and no priority logic sits in the next-state path.
3. **The SRAM's address register is the state register.** The entry read on one clock chooses the address
   the SRAM latches at the end of that clock, and the new entry is out on the next. That gives one decision per
   clock with a synchronous read, which the simulator models (`q_addr` in [chip.py](chip.py)). The loop is
   SRAM output, a 16:1 condition select, an OR, a 2:1 mux, SRAM address. **It is not checked in synthesis.**
   If it cannot close at 60 MHz, decisions drop to every second clock and Ethernet is lost (reasoned, not run).
4. **Actions belong to the state** (a Moore machine): they run on every clock spent in it. So a line that
   waits may only set pins, and "do this, then wait" is two states. Pin operations alone can be gated by an
   input in the clock that sees it (`when`), which gives the pads' floor of 3 clocks from pin to pin.
5. **Up to four machines take turns** on the one read port. One machine decides every clock, two decide every
   second clock, four every fourth. Each has its own registers; the datapath logic is shared. Timers count
   turns, so a machine's timing stays exact.
6. **One subroutine level.** `call` loads a 9-bit link register with the return entry, and `ret` jumps to it.

### The table entry (64 bits)

| Word A | Bits | Meaning | Word B | Bits | Meaning |
|---|---|---|---|---|---|
| `base` | 9 | Successor block | `imm` | 16 | T, or J [15:12], K [11:8], a byte [7:0] |
| `cA`, `cB` | 4 + 4 | Conditions OR-ed into the address | `src`, `dst` | 3 + 3 | One byte moves on the bus |
| `p0`, `p0op` | 2 + 3 | First pin and its operation | `shift` | 1 | Shift S, the serial input enters |
| `p1`, `p1op` | 2 + 3 | Second pin and its operation | `crc` | 3 | LFSR: clear, init, step, shift out |
| `gate` | 2 | Pins change always, if cA, if not cA | `nop` | 2 | N: keep, dec, inc. Code 3 is `ret` |
| `dsel` | 1 | Data bit from S or from the LFSR | `kj` | 3 | K and J: keep, dec or load each |
| `din` | 2 | Serial input: 0, 1, input A, input B | `tload` | 1 | T = imm |

### The datapath of one machine

| Register | Bits | What it does |
|---|---|---|
| T | 16 | Timer. Loaded from `imm`, counts down every turn, stops at 0 (`tz`). Up to 1.09 ms. |
| N | 16 | Counter, up or down (`nz`). Loaded and read a byte at a time through the bus. |
| K, J | 4 + 4 | Down-counters (`kz`, `jz`): bits in a byte, bytes in a group, 1s since the last stuffed bit. |
| S | 8 | Shifter. `order` picks the end that is the data bit `d`. The bus loads and reads it. |
| R | 8 | A second byte register. |
| LFSR | 32 | Right-shifting, with programmable polynomial, start value and match value (`crc`). |
| link | 9 | Return address. |

The 16 conditions are: inputs A, B and C, `edge` (A changed since the last turn), `tz`, `kz`, `jz`, `nz`, the
data bit, `crc`, `rx0` and `rx3` (a byte waits on channel 0 or 3), `tx1` and `tx2` (channel 1 or 2 has room),
`S == imm` and constant 0. The 8 pin operations are: keep, 0, 1, release, data bit, inverted data bit,
toggle, and open drain (release for 1, pull low for 0). The `crc` flag is one turn behind the LFSR, and
`edge` means nothing in a machine's first clocks after reset, so a program that waits on it settles first.

## The language

One line is one state and one clock: `[label:] actions [=> rules]`. A program reads top to bottom, and the
author counts clocks by counting lines. This is the whole UART transmitter:

```
machine tx
    out   TX=P0
    order lsb
    const BIT = 521                  # 60 MHz / 115200

idle:   TX=1, wait rx0               # the line idles high until a byte is waiting
        S=pop0, K=8
        TX=0, hold BIT               # start bit
bit:    TX=d, shift, K-=1, hold BIT => kz: stop | bit
stop:   TX=1, hold BIT => idle
```

| Form | Meaning |
|---|---|
| `machine NAME`, `in A=P1, ...`, `out X=P0, ...` | A machine, its inputs (at most 3) and outputs (at most 4) |
| `order lsb`, `crc poly=.. init=.. match=..`, `const` | Shift order, LFSR setup, named numbers |
| `X=0`, `X=1`, `X=z`, `X=toggle` | Drive low, drive high, release, invert |
| `X=d`, `X=!d`, `X=od`, `X=c`, `X=!c` | Follow the shifter's data bit or the LFSR's low bit |
| `X=z when CS` | A pin operation that happens only in a clock where the condition holds |
| `T=n`, `K=n`, `J=n`, `K-=1`, `J-=1`, `N-=1`, `N+=1` | Timer and counters |
| `shift`, `shift RX`, `shift 1` | Shift S; a 0, an input or a 1 enters |
| `crc=0`, `crc=init`, `crc+=d`, `crc+=RX`, `crc>>=1` | LFSR: clear, start value, step with a bit, shift out |
| `S=pop0`, `push1=S`, `N=pop0`, `push2=Nlo`, `S=0x55` | One byte from a source to a destination |
| `=> kz & nz: a \| kz: b \| c` | Rules: the first true guard wins. With none true, the next line follows |
| `wait`, `wait rx0` | Stay in this state until a rule fires, or until the condition holds |
| `hold n` | This line lasts n clocks in all (the compiler adds a timer load and a wait state) |
| `call L`, `ret`, `goto L`, `nop`, `halt` | Subroutine, jump, empty clock, stop |
| `macro NAME(P) ... end`, `use NAME(x)` | Text macros with their own labels |

Limits the compiler reports with the line: two conditions per state, two pins per state, one bus move per
state, and one immediate per state (`T=` cannot share a line with `K=`, `J=` or a constant byte).

## Budget

| Per machine | Flops | Shared | Flops |
|---|---|---|---|
| State and link | 18 | Host FIFOs, 4 x 4 bytes, with pointers | 156 |
| T and N | 32 | Pad output and enable registers | 16 |
| K, J, S, R | 24 | Input synchronisers (modelled by the harness) | 16 |
| LFSR, polynomial, start and match values | 128 | Turn counter, machine count, valid, run | 6 |
| Flags and the previous level of input A | 6 | Loader | 48 |
| Pin map (4 out, 3 in) and shift order | 22 | Host link | 40 |
| **One machine** | **230** | **Shared** | **282** |

1. **Flops: 1,202** (4 x 230 + 282) of the 3,000 allowed.
2. **SRAM: 32,768 bits,** both 512x32 macros, all of it the state table.
3. **Gates: about 2,600 combinational cells.** This is an estimate, not a synthesis result: 680 for the
   per-machine register muxes and enables, 600 for the shared datapath, 130 for the FIFOs, 320 for the loader
   and host link, and half as much again as margin.
4. **Protocols at once: four,** each machine at 15 MHz. Two run at 30 MHz each (uart_3m). The self-check runs
   UART transmit, UART receive, an SPI controller and a PS/2 receiver together from
   [four_at_once.fsm](programs/extra/four_at_once.fsm). Ethernet needs all 60 MHz, so it runs alone.

## Pins on Tiny Tapeout

| Pins | Use |
|---|---|
| `uio[7:0]` | P0 to P7, the protocol pins: the only pads that can be both input and output |
| `ui_in[3:0]` | Host data in, one nibble at a time |
| `ui_in[5:4]` | Port: 0 is channels 0 and 1, 1 is channels 3 and 2, 2 is the loader |
| `ui_in[6]`, `ui_in[7]` | Write strobe and read strobe; each edge moves one nibble |
| `uo_out[3:0]` | Host data out |
| `uo_out[7:4]` | The selected write channel has room, the read channel has a byte, running, overflow |

A strobe edge takes two clocks to synchronise, so the RP2350 can move a nibble every 4 clocks and a byte every
8. Two channels each way then get one byte per 16 clocks, which is the bake-off's host rate. The loader
takes the image over the same port: the machine count, 16 bytes of setup per machine, then the entries from
address 0. The largest image loads in under 1 ms. The host link is not modelled; the harness stands in for it.

## What it does that a PIO cannot

1. **A four-way branch and eight register operations in one clock, every clock.** A PIO runs one instruction
   per clock and tests one thing per instruction. In `eth10_tx`, the clock between two bytes tests the bit
   count and the byte count, and the next clock fetches a byte, reloads one counter and steps another, while
   the pins keep exact 3-clock half bits.
2. **CRC and parity on chip.** One LFSR does CRC-8, CRC-16, CRC-32 and PS/2 parity. A PIO has none.
3. **Clock recovery at 6 clocks per bit.** `eth10_rx` re-times on every edge, takes the bit one clock later,
   matches the start-of-frame byte, checks the CRC and counts the length, in 21 states.
4. **Bit stuffing and NRZI in the table,** with a counter of 1s that a stuffed bit resets (both USB scenarios).
5. **Pin to pin in 3 clocks,** the pads' floor, for a gated pin operation (measured by the self-check).
6. **512 states for all machines,** against 32 instructions.

A PIO is ahead on program size (2 bytes per instruction against 8 per state), on 32-bit shifters with
automatic push and pull, on fractional clock dividers, and on running every state machine at full speed.

## Helpers that serve fewer than two scenarios

1. **`R`** (i2c_ctrl) keeps the address, which is sent twice, then the status. One shifter cannot keep it.
2. **Input C** (spi_target_m0) is the third input a three-wire target needs.
3. **`when`** (spi_target_m0) releases MISO 3 clocks after CS rises. The bench also passes without it, at 4.
4. **`S == imm`** (eth10_rx) finds a sync byte or an address without a state per bit.
5. **`toggle`** (usb_ls_tx) is NRZI, and **`!d`** (eth10_tx) drives the second wire of a differential pair.
6. **`rx3`, `pop3`, `tx1`, `tx2`** serve no scenario. Channel 3 is in the contract and the four-machine
   self-check uses it. `tx1` and `tx2` let a program wait for the host; no scenario fills a 4-byte FIFO.

## Failing scenarios

None. These limits showed up on the way and would bite on other protocols:

1. **The 60 MHz loop through the SRAM is unproven.** See point 3 of "The machine".
2. **Programs are big.** A state is 8 bytes. `i2c_ctrl` was 1,195 bytes before `call` and `ret` existed.
3. **K and J hold 15 at most and S holds 8 bits.** Longer words need nested counters and a byte per bus move.
4. **Several machines share the decision rate.** Two fast protocols at once do not fit: Ethernet runs alone.
5. **One subroutine level,** so a subroutine cannot call another one.
6. **A pushed byte is dropped when its FIFO is full,** unless the program tests `tx1` or `tx2` first.
