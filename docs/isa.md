# protoemu instruction set

This is the programmer's reference for protoemu: the 64-bit table entry, the machine configuration, the image
format, the state-machine language that produces them, the compiler's limits and messages, and how to run a
program on the model and load it into the chip. [architecture.md](architecture.md) explains why the chip is
built this way, and [programming.md](programming.md) walks through writing a new protocol.

Every encoding below comes from [entry.py](../bakeoff/cand_table/entry.py) and
[compiler.py](../bakeoff/cand_table/compiler.py), and every behaviour from the golden model
[chip.py](../bakeoff/cand_table/chip.py), which the RTL ([protoemu_core.v](../src/protoemu_core.v)) matches on
every clock.

1. [How a machine steps](#1-how-a-machine-steps)
2. [The table entry](#2-the-table-entry)
3. [Machine configuration and the image](#3-machine-configuration-and-the-image)
4. [The state-machine language](#4-the-state-machine-language)
5. [What the compiler builds](#5-what-the-compiler-builds)
6. [Limits and error messages](#6-limits-and-error-messages)
7. [Compile, run on the model, load into the chip](#7-compile-run-on-the-model-load-into-the-chip)

## 1. How a machine steps

A program is a table of states. One state is one 64-bit table entry. Up to four machines walk the table, each
with its own registers, and they take turns: with n machines, each machine takes one step every n clocks. One
step of one machine is called a **turn**. With one machine, a turn is a clock.

In one turn, in this order:

1. The entry of the machine's current state is on the SRAM outputs.
2. Every condition, the data bit `d` and every value the entry reads come from the machine's registers as they
   stand at the start of the turn. Input pins come through a two-flop synchroniser, so a turn sees each pin as
   it was two clocks earlier.
3. The next state is chosen: `base | cA | cB << 1`, where `cA` and `cB` are the values of the entry's two
   selected conditions. An entry whose N field is 3 (a return) goes to the machine's link register instead.
4. All of the entry's operations happen together at the end of the turn: up to two pin operations, one byte
   moved on the bus, one shift, one LFSR operation, and updates of N, K, J and T.
5. A pin operation reaches the pad one clock later (the pad's output register).

So a state's actions run on every turn spent in it (a Moore machine), and a state's conditions never see its own
actions: an entry that does `K-=1` and tests `kz` tests K as it was before the decrement.

## 2. The table entry

An entry is two 32-bit words at the same address: word A in the first 512x32 SRAM macro and word B in the
second.

```
Word A (control)
  31 30 | 29   | 28 27 | 26 25 24 | 23 22 | 21 20 19 | 18 17 | 16 15 14 13 | 12 11 10  9 | 8 ......... 0
  din   | dsel | gate  | p1op     | p1    | p0op     | p0    | cB          | cA          | base

Word B (datapath)
  31    | 30 29 28 | 27 26 | 25 24 23 | 22    | 21 20 19 | 18 17 16 | 15 ................................ 0
  tload | kj       | nop   | crc      | shift | dst      | src      | imm
```

| Field | Bits | Meaning |
|---|---|---|
| `base` | A[8:0] | The successor block's address |
| `cA` | A[12:9] | Condition OR-ed into address bit 0; it is also the gate of the pin operations |
| `cB` | A[16:13] | Condition OR-ed into address bit 1 |
| `p0`, `p0op` | A[18:17], A[21:19] | First pin operation: logical output 0 to 3, and the operation |
| `p1`, `p1op` | A[23:22], A[26:24] | Second pin operation |
| `gate` | A[28:27] | 0: pin operations always happen; 1: only if `cA` is true; 2: only if `cA` is false; 3: never (the compiler does not emit it) |
| `dsel` | A[29] | The data bit `d`: 0 from the shifter S, 1 from bit 0 of the LFSR |
| `din` | A[31:30] | The serial input: 0 a 0, 1 a 1, 2 input A, 3 input B |
| `imm` | B[15:0] | The immediate: T's load value, or J in [15:12], K in [11:8] and a byte in [7:0] |
| `src` | B[18:16] | Bus source |
| `dst` | B[21:19] | Bus destination |
| `shift` | B[22] | Shift S one place; the serial input enters |
| `crc` | B[25:23] | LFSR operation |
| `nop` | B[27:26] | N: 0 keep, 1 decrement, 2 increment, 3 return |
| `kj` | B[30:28] | K and J: keep, decrement or load each |
| `tload` | B[31] | Load T from `imm` |

An all-zero entry does nothing and goes to entry 0. The loader fills every entry above a program's end with
zeros.

### 2.1 The next address

The next address is `base | cA | cB << 1`, built with an OR and no adder. So a state's successors sit side by
side in the table: a state that tests two conditions owns a block of four entries at a `base` that is a multiple
of 4, and the four successors are, in order, "neither", "cA only", "cB only" and "both". A state that tests one
condition owns a block of two. A state with no condition (`cA = cB = 0`, constant 0) goes to `base`.

A return (`nop` = 3) ignores `base` and goes to the address in the machine's link register. Its `cA` still
selects a condition, which can gate its pin operations (section 2.3). A call loads the link register: `dst` = 7
copies `imm[8:0]` into it.

### 2.2 Conditions

`cA` and `cB` each pick one of 16 flags. All of them describe the machine's state at the start of the turn.

| Code | Language | True when |
|---|---|---|
| 0 | (none) | Never: constant 0. An unused condition select is 0. |
| 1 | the first `in` name | Input A is high |
| 2 | the second `in` name | Input B is high |
| 3 | the third `in` name | Input C is high |
| 4 | `edge` | Input A differs from its level at this machine's previous turn. After reset that remembered level is 1, so a machine whose input A is low sees `edge` on its first turn. |
| 5 | `tz` | T is 0 |
| 6 | `kz` | K is 0 |
| 7 | `jz` | J is 0 |
| 8 | `nz` | N is 0 |
| 9 | `d` or `c` | The data bit is 1: `d` is the shifter's end bit (`dsel` = 0), `c` is LFSR bit 0 (`dsel` = 1) |
| 10 | `crc` | The LFSR equalled the configured match value at the start of the previous turn. The compare is registered, so this flag is one turn behind the LFSR. |
| 11 | `rx0` | Host channel 0 holds a byte |
| 12 | `rx3` | Host channel 3 holds a byte |
| 13 | `tx1` | Host channel 1 has room (fewer than 4 bytes) |
| 14 | `tx2` | Host channel 2 has room |
| 15 | `S==n` | S equals `imm[7:0]` |

The language also allows a negated condition, `!kz`. The hardware has no inverter for it: the compiler puts the
successors in the other slots of the block.

### 2.3 Pin operations

An entry has two pin operations, `p0` and `p1`. Each names a logical output, 0 to 3, which the machine's
configuration maps to one of the pins P0 to P7. The chip has one output register and one output-enable register
for all eight pins, shared by every machine. After reset every pin is released (an input).

| Code | Language | Output | Output enable |
|---|---|---|---|
| 0 | (none) | kept | kept |
| 1 | `X=0` | 0 | 1 (driven) |
| 2 | `X=1` | 1 | 1 |
| 3 | `X=z` | kept | 0 (released) |
| 4 | `X=d`, `X=c` | the data bit | 1 |
| 5 | `X=!d`, `X=!c` | the inverted data bit | 1 |
| 6 | `X=toggle` | inverted | 1 |
| 7 | `X=od` | 0 | the inverted data bit: a 1 releases the pin, a 0 pulls it low (open drain) |

1. The data bit is taken before this turn's shift: `S[0]` with `order lsb`, `S[7]` with `order msb`, or LFSR bit
   0 when `dsel` is 1.
2. When both operations name the same pin, `p1` acts after `p0`.
3. The `gate` field makes both operations depend on condition `cA` in this same turn. This is the only gated
   operation in the entry: everything else always happens.

### 2.4 The byte bus

One byte moves per turn, from `src` to `dst`.

| `src` | Source | | `dst` | Destination |
|---|---|---|---|---|
| 0 | `imm[7:0]` | | 0 | nothing |
| 1 | pop channel 0 | | 1 | S |
| 2 | pop channel 3 | | 2 | R |
| 3 | S | | 3 | N: `N = byte`, so the high byte becomes 0 |
| 4 | R | | 4 | the high byte of N; the low byte is kept |
| 5 | N[7:0] | | 5 | push to channel 1 |
| 6 | N[15:8] | | 6 | push to channel 2 |
| 7 | nothing (0) | | 7 | the link register: `link = imm[8:0]`, not the bus (a call) |

1. A pop happens whenever `src` is 1 or 2, even with no destination. A pop from an empty channel gives 0 and
   changes nothing.
2. A push into a full channel (4 bytes) drops the byte and sets the chip's OVERFLOW flag. Test `tx1` or `tx2`
   first when that matters.
3. Channels 0 and 3 run from the host to the machines, 1 and 2 from the machines to the host. All four are shared
   by every machine.

### 2.5 The shifter and the serial input

`shift` moves S one place, and the serial input chosen by `din` enters at the other end:

1. With `order lsb`, `S = {in, S[7:1]}`. The data bit is `S[0]`, so bits leave LSB first, and the first bit
   received ends in `S[0]` after eight shifts.
2. With `order msb`, `S = {S[6:0], in}`. The data bit is `S[7]`.

`din` also feeds the LFSR's `step with the serial input` operation. Inputs A and B can be shifted in; input C
cannot.

### 2.6 The LFSR

Each machine has a 32-bit LFSR with three configured values: the polynomial, the start value (`init`) and the
match value.

| Code | Language | Operation |
|---|---|---|
| 0 | (none) | Keep |
| 1 | `crc=0` | Clear to 0 |
| 2 | `crc=init` | Load the start value |
| 3 | `crc+=d` | Step with the data bit `d` |
| 4 | `crc+=0`, `crc+=1`, `crc+=X` | Step with the serial input (`din`) |
| 5 | `crc>>=1` | Shift right by one with no feedback |
| 6, 7 | (none) | Step with the serial input, like 4; the compiler does not emit them |

A step is `fb = LFSR[0] ^ bit; LFSR = LFSR >> 1; if fb: LFSR ^= poly`. The LFSR shifts right, so a polynomial is
written bit-reversed ("reflected"): CRC-8 with x^8 + x^5 + x^4 + 1 is `0x8C`, CRC-16 with x^16 + x^15 + x^2 + 1
is `0xA001`, CRC-32 is `0xEDB88320`. A CRC defined most significant bit first works the same way when its
polynomial, start and match values are all written bit-reversed and the bits are fed in the order its definition
takes them. A one-bit LFSR (`poly=1`) is a running parity.

To send a CRC, drive a pin with `c` or `!c` (LFSR bit 0) and move to the next bit with `crc>>=1`. To check one
received, step the CRC bits through the LFSR too and test the `crc` flag against the residue in `match`.

### 2.7 Counters and the timer

| Register | Width | Changed by | Flag |
|---|---|---|---|
| T | 16 | `tload`: `T = imm`. Otherwise T counts down by 1 every turn of its machine, in every state, and stops at 0. | `tz` |
| N | 16 | `nop`: decrement or increment, wrapping at 16 bits. The bus writes it a byte at a time. | `nz` |
| K | 4 | `kj`: load `imm[11:8]`, or decrement, wrapping from 0 to 15 | `kz` |
| J | 4 | `kj`: load `imm[15:12]`, or decrement, wrapping from 0 to 15 | `jz` |

The `kj` code is `3 * k + j`, where each of `k` and `j` is 0 keep, 1 decrement or 2 load. Codes 0 to 7 exist,
so K and J can never both be loaded in one entry.

| `kj` | K | J |
|---|---|---|
| 0 | keep | keep |
| 1 | keep | decrement |
| 2 | keep | load |
| 3 | decrement | keep |
| 4 | decrement | decrement |
| 5 | decrement | load |
| 6 | load | keep |
| 7 | load | decrement |

T, K, J and the byte constant share `imm`. T uses all 16 bits, so it can share an entry with K, J or a byte only
when the bits agree.

### 2.8 Inside one turn

Everything reads the registers as they stood at the start of the turn. Where one entry writes a register two
ways, this is what the register gets:

1. S: a bus write to S replaces the shift. (The compiler refuses both in one state.)
2. N: the increment or decrement happens first. A bus write to N then replaces all of it, and a write to the
   high byte replaces the high byte and keeps the new low byte. (The compiler refuses `N=` with a count in one
   state, and allows `Nhi=` with one.)
3. T: a load replaces the countdown.

The flags `tz`, `kz`, `jz` and `nz` are registered beside their registers, so on any turn they describe the
registers at its start. The `crc` flag is one turn later still (section 2.2).

## 3. Machine configuration and the image

### 3.1 Configuration: 16 bytes per machine

| Byte | Bits | Meaning |
|---|---|---|
| 0 | [2:0], [5:3] | The pins of logical outputs 0 and 1 (0 to 7 for P0 to P7) |
| 1 | [2:0], [5:3] | The pins of logical outputs 2 and 3 |
| 2 | [2:0], [5:3] | The pins of inputs A and B |
| 3 | [2:0], [3] | The pin of input C; the shift order (0 `lsb`, 1 `msb`) |
| 4 to 7 | | The LFSR polynomial, low byte first |
| 8 to 11 | | The LFSR start value (`init`), low byte first |
| 12 to 15 | | The LFSR match value, low byte first |

An output or input the program does not declare maps to P0. Several machines may use the same pin, and one
machine may use one pin as both an output and an input (the I2C and 1-Wire programs do).

### 3.2 The image

```
[machine count: 1 byte]
[16 configuration bytes] x machine count
[entry count: 2 bytes, low byte first]
[8 bytes per entry: word A then word B, each low byte first] x entry count, from address 0
```

Machine m starts in entry m. The loader writes zeros to every entry above the count, so no old state is left.
An image is 1 + 16 x machines + 2 + 8 x entries bytes: the bench programs are 107 to 771 bytes. The loader's
handling of out-of-range counts is in [info.md](info.md#loading-a-program).

## 4. The state-machine language

A program is a text file, by convention `.fsm`. The examples here come from
[bakeoff/cand_table/programs/](../bakeoff/cand_table/programs/) and [docs/examples/](examples/). This is the
whole UART transmitter, [uart_tx.fsm](../bakeoff/cand_table/programs/uart_tx.fsm):

```
machine tx
    out   TX=P0
    order lsb
    const BIT = 521                  # 60 MHz / 115200 = 520.83 clocks

idle:   TX=1, wait rx0               # the line idles high until a byte is waiting
        S=pop0, K=8
        TX=0, hold BIT               # start bit
bit:    TX=d, shift, K-=1, hold BIT => kz: stop | bit
stop:   TX=1, hold BIT => idle
```

### 4.1 Lines, comments and labels

1. One line holds one statement. `#` starts a comment that runs to the end of the line. Blank lines are ignored.
   Indentation means nothing.
2. A **state line** is one state, and one turn: `[label:] actions [=> rules]`. The source reads top to bottom,
   and a state with no rule that fires goes to the next state line.
3. A **label** is a name and a colon at the start of a line. It may stand alone on its line, and then it names
   the next state line. Several labels may name one line. Labels belong to their machine: two machines may
   both have an `idle`.
4. A label may not be a reserved word (section 4.12), inside a macro too, or a pin name declared above it.

### 4.2 Declarations

| Statement | Meaning |
|---|---|
| `const NAME = expr` | A named number. Constants are global, may appear anywhere, and must be defined above their first use. A constant is rounded to an integer when it is defined. It may be defined again, and a line uses the latest definition above it. |
| `machine NAME` | Starts a machine's section. Everything below belongs to it until the next `machine`. At most 4. The name is optional (`m0`, `m1`, ...). Machine m starts at its first state line, which may not be a `goto`. |
| `out X=P0, Y=P1` | The machine's outputs, at most 4 in all, in the order logical output 0, 1, 2, 3. Several `out` lines add up. |
| `in A=P1, B=P2` | The machine's inputs, at most 3 in all: the first is input A, then B, then C. Only A has `edge`, and only A and B can be shifted in. |
| `order lsb` or `order msb` | Which end of S is the data bit. The default is `lsb`. A machine sets it once at most. |
| `crc poly=v init=v match=v` | The LFSR's values. Any of the three, in any order; the default of each is 0. Each value is an expression (section 4.3) that runs to the next `poly=`, `init=` or `match=`, so `crc init = 0xFF00 + 0xFF` sets 0xFFFF. It must come to 0 to 0xFFFFFFFF. A machine sets each of the three once at most, on one line or on several. |

A pin or constant name may be any name that is not reserved and does not start with a digit. A machine declares
a pin name once as an `in` and once as an `out` at most, above the lines that use it. One name may be both an
`in` and an `out` of the same pin: as a condition it reads the input, and on the left of `=` it drives the output.

### 4.3 Numbers and expressions

Wherever a number goes, an expression may: decimal, hex (`0x8C`), binary (`0b1010`), `1_000` and floats
(`60e6`), constants, `+ - * / // %`, unary minus, brackets, and the functions `round`, `int`, `min` and `max`.
Nothing else is evaluated (no `**`, no names other than constants), so compiling never runs code a program
carries. The result is rounded to the nearest integer, halves to even: `5/2` is 2 and `7/2` is 4. A result over
1,024 bits is refused.

### 4.4 Actions

A state line lists its actions separated by commas. They all happen in the same turn. A comma inside brackets
belongs to the expression: `T=max(BIT, 4), K=8` is two actions.

| Action | What it does | Entry fields |
|---|---|---|
| `X=0`, `X=1` | Drive output X low or high | `pN`, `pNop` |
| `X=z` | Release X (stop driving it) | |
| `X=toggle` | Drive X to the inverse of its output register | |
| `X=d`, `X=!d` | Drive X with the shifter's data bit, or its inverse | `dsel` = 0 |
| `X=c`, `X=!c` | Drive X with LFSR bit 0, or its inverse | `dsel` = 1 |
| `X=od` | Open drain from the shifter's data bit: release for 1, pull low for 0 | `dsel` = 0 |
| `X=... when COND` | The pin operation happens only in a turn where COND holds (section 4.6) | `gate`, `cA` |
| `T=expr` | Load the timer, 0 to 65,535 turns | `tload`, `imm` |
| `K=expr`, `J=expr` | Load a 4-bit counter, 0 to 15 | `kj`, `imm` |
| `K-=1`, `J-=1` | Count down (K and J only count down) | `kj` |
| `N-=1`, `N+=1` | Count N down or up | `nop` |
| `DST=SRC` | Move a byte. DST is `S`, `R`, `N`, `Nhi`, `push1` or `push2`; SRC is `pop0`, `pop3`, `S`, `R`, `Nlo` or `Nhi`. For example `S=pop0`, `push1=S`, `Nhi=pop0`. | `src`, `dst` |
| `DST=expr` | Move a byte constant, 0 to 255, to DST: `S=0x55` | `src` = 0, `dst`, `imm` |
| `pop0`, `pop3` | Pop a byte from channel 0 or 3 and drop it | `src` |
| `shift` | Shift S, with a 0 entering | `shift`, `din` = 0 |
| `shift 0`, `shift 1` | Shift S, with that bit entering | `din` |
| `shift X` | Shift S, with input X entering; X is the machine's first or second `in` | `din` = 2 or 3 |
| `crc=0`, `crc=init` | Clear the LFSR, or load its start value | `crc` |
| `crc+=d` | Step the LFSR with the shifter's data bit | `crc` = 3 |
| `crc+=0`, `crc+=1`, `crc+=X` | Step the LFSR with a constant or with input X (first or second `in`) | `crc` = 4, `din` |
| `crc>>=1` | Shift the LFSR right with no feedback | `crc` = 5 |
| `nop` | Nothing. A line holding only `nop` is an empty turn. | |
| `wait`, `wait GUARD` | Stay in this state (section 4.8) | |
| `halt` | Stay in this state for good, unless a rule on the line fires | |
| `hold expr` | This line lasts that many turns in all (section 4.9) | |
| `call L`, `ret` | Subroutine call and return (section 4.10) | `dst` = 7, `nop` = 3 |

In one state:

1. At most two pin operations. Two on one pin act in the order written.
2. One bus move: `DST=SRC`, `DST=expr`, `pop0`, `pop3` and `call` each use the bus.
3. One shift, one LFSR operation, one change of N, one change of K and one of J, and K and J are not both
   loaded. `N=` changes N, as `N-=1` does. `Nhi=` writes only N's high byte, so it may share a state with a count.
4. One serial input: `shift X` and `crc+=Y` must name the same input, and plain `shift` counts as `shift 0`.
5. One data bit: `d` (the shifter) and `c` (the LFSR) cannot both appear, in actions or conditions.
6. S is shifted or written by the bus, not both.
7. One immediate: `T=`, `K=`, `J=`, a byte constant and `S==n` share `imm`. K, J and the byte use separate bits;
   T uses all 16, so it shares a line with them only when the bits agree.

### 4.5 Conditions in the language

A condition is one of:

1. An `in` name: the level of that input.
2. `edge`, `tz`, `kz`, `jz`, `nz`, `crc`, `rx0`, `rx3`, `tx1`, `tx2` (section 2.2). `edge` watches input A, so
   the machine must declare an `in` pin above it.
3. `d` or `c`: the data bit from the shifter or from the LFSR.
4. `S==expr`: S equals a byte constant.

A `!` in front negates it, and two cancel: `!!kz` is `kz`, as when a macro's `!P` is given `!kz`. One state may
test at most **two different conditions** in all: the ones in its rules, in `wait GUARD` and in `when` together.
`kz & !tz` and `tz` are two conditions. A `hold` line's rules are tested by a state of their own (section 4.9).

### 4.6 `when`: gating pin operations

`X=z when CS` releases X only in a turn where CS is high. The condition is one condition, possibly negated, and
every pin operation in the state must carry the same `when` or none must. It becomes the state's `cA`, so it
also counts as one of the state's two conditions, and it acts in the same turn that sees it, which is one turn
earlier than a rule that moves to a state that sets the pin. Only pin operations can be gated. A `call` or `ret`
line may gate its pins too: the call or the return happens either way.

```
low:    MISO=z when CS, wait => CS: start | SCK: rise      # spi_target_m0.fsm
```

### 4.7 Rules

`=> rule | rule | ...` follows the actions. A rule is `guard: label`, and a guard is one or more conditions
joined with `&`. The rules are tried in order and the first whose guard holds chooses the next state. With none
true, the next state line follows (or the state stays, with `wait` or `halt`).

| Rule | Meaning |
|---|---|
| `kz: stop` | Go to `stop` if K is 0 |
| `kz & !nz: more` | Go to `more` if K is 0 and N is not |
| `else: idle` | Always go to `idle`; it is useful as the last rule |
| `idle` | The same as `else: idle` |

There is no "or" inside a guard: write two rules with the same label. A line may hold only rules: `=> kz: done`
is one turn that does nothing and branches.

Rules choose from the values at the start of the turn, so they do not see the line's own actions. A line that
counts with `K-=1` and must branch on the result tests `kz` on the next line, or on a `hold` line of 2 turns or
more (section 4.9), whose rules are tested on its last turn.

### 4.8 `wait` and `halt`

1. `wait` keeps the machine in this state until a rule fires: `wait => edge: bit | tz: end`.
2. `wait GUARD` stays until GUARD holds, then goes to the next line: `wait !RX`. With rules, GUARD is added to
   every rule, and the next line follows when GUARD holds and no rule does: `wait rx0 => kz: a`.
3. `halt` stays for good. Rules on a `halt` line still apply, as with `wait`.
4. A waiting state repeats its actions on every turn, so it may only set pins, and not with `toggle`. It may not
   move a byte, shift, step the LFSR, or change N, K, J or T. "Do this, then wait" is two lines.
5. The timer keeps counting down while a machine waits, so `T=n` on one line and `wait tz` on the next waits
   n + 1 turns there, n + 2 turns for the pair.
6. A line has one `wait` or one `halt`: join the conditions of a `wait` with `&`.

### 4.9 `hold n`

`hold n` makes the line last n turns in all. Its actions happen on the first turn only. Pins it set stay as they
are for the rest, because an output keeps its level until something changes it. Its rules are tested on the last
turn, so with n of 2 or more they see the line's own actions. The `crc` flag is one turn behind (section 2.2), so
rules see the line's own LFSR step with n of 3 or more.

The compiler builds the extra turns from states named after the line:

1. `hold 1` is the line itself.
2. `hold 2` adds a `.pad` state, which carries the rules.
3. `hold n` with n of 3 or more loads `T = n - 2` on the line and adds a `.hold` state that waits for `tz`. The
   rules move to the `.hold` state with `tz` added to every guard, so a hold line's rules may test only one
   condition besides the timer.
4. If the line's immediate (`K=`, `J=` or a byte constant) has other bits than n - 2, the timer load moves to a
   `.load` state (`T = n - 3`), and `hold 3` becomes the line and two `.pad` states, with no load of T.
5. Rules on a `.pad` state have no timer to test, so they may test two conditions.
6. A `when` on the line gates its first turn's pin operations, and does not count against the rules' conditions.

A line has one `hold`, and no `wait` or `halt` beside it. With n of 2 or more it may not test `S==`, and with n
of 3 or more it may not load T itself. `hold` works up to 65,537 turns (T = 65,535). A longer time needs a loop
that counts with K, J or N.

### 4.10 `call` and `ret`

`call L` goes to L and loads the link register with the entry of the line after the call. `ret` goes back there.
The actions on both lines run as usual.

1. There is one link register per machine, so a subroutine cannot call another: the second call overwrites the
   return address.
2. A `call` line has no rules, `wait`, `halt`, `hold` or `ret`, and nothing else that uses the immediate or the
   bus (the return address travels in `imm`).
3. A `ret` line has no rules, `wait`, `halt`, `hold` or `call`, and may not count N: the N field holds the return
   code, so the compiler refuses `N-=1` or `N+=1` on a `ret` line, in either order. Change N on the line before.
   `N=` is a bus write, which a `ret` line may have.
4. A `ret` line may gate its pin operations with `when`: `X=1 when A, ret` drives X in a turn where A is high,
   and returns either way.

The I2C controller ([i2c_ctrl.fsm](../bakeoff/cand_table/programs/i2c_ctrl.fsm)) shares the second half of
every clock pulse this way, and [usb_ls_tx.fsm](../bakeoff/cand_table/programs/usb_ls_tx.fsm) sends SYNC and the
PID through one subroutine.

### 4.11 `goto` and macros

`goto L` stands alone on its line. It is not a state and takes no turn: the line above it continues at L
instead of at the line below. It cannot share a line with actions. To jump after actions, use a rule: `nop => L`.
A machine cannot start with a `goto`, as no line is above it: start with `nop => L`.

A macro is a block of lines with parameters, pasted where it is used:

```
macro pulse(SET)                 # i2c_ctrl.fsm
        hold 40
        SET, call clock
end

wbit:   use pulse(SDA=od)
```

1. `macro NAME(P, Q)` starts a definition and a line holding only `end` closes it. Definitions may sit anywhere,
   before or after their use, inside or outside a machine's section, but not inside another definition. A macro
   is defined once, and its parameters are names, no two the same.
2. `use NAME(a, b)` pastes the body, with each parameter replaced by its argument wherever it appears as a whole
   word (so not the `F` of `0xF`). The argument is pasted as text: `T=V*2` given `1+2` is `T=1+2*2`, so give it
   `(1+2)`. Commas inside brackets or braces do not separate arguments, and the braces are dropped:
   `use nrzi(d, {shift, crc+=d}, more)`.
3. A label defined inside the macro gets a new name at every use, so one macro can be used many times. A label
   on the `use` line names the first line of the body.
4. Macros may use other macros, up to 8 deep.

### 4.12 Reserved words

These cannot be names of pins, constants or labels: `edge`, `tz`, `kz`, `jz`, `nz`, `crc`, `rx0`, `rx3`, `tx1`,
`tx2`, `S`, `R`, `N`, `Nhi`, `Nlo`, `push1`, `push2`, `pop0`, `pop3`, `T`, `K`, `J`, `d`, `c`, `wait`, `hold`,
`goto`, `nop`, `halt`, `shift`, `else`, `when`, `use`, `call` and `ret`.

## 5. What the compiler builds

1. It reads the lines, takes out the macro definitions, and pastes every `use`.
2. It parses each line into a state. `hold` adds the states of section 4.9.
3. It works out every state's successors: for the two conditions it tests, the state each of the four
   combinations leads to. A condition that changes nothing is dropped, unless it is the gate of a `when`.
4. It places the blocks. Each state's successors form a block of 1, 2 or 4 entries at an address that is a
   multiple of the block's size. A state that is a successor in several blocks is copied into each of them, so
   one state can occupy several entries. Machine m's first state is fixed at entry m. The biggest blocks are
   placed first, and a block reuses entries that already hold the same states where it can.
5. States no machine can reach are left out, with a warning.
6. It packs every entry and writes the image.

### 5.1 The listing

`python3 -m bakeoff.cand_table.compiler FILE` prints one line per entry. This is
[uart_tx.fsm](../bakeoff/cand_table/programs/uart_tx.fsm):

```
bakeoff/cand_table/programs/uart_tx.fsm: 8 states in 11 entries (0 unused), image 107 bytes
  0  tx.idle                TX=1                                     -> 0 | rx0 [idle line9]
  1  tx.line9               S=pop0, K=8                              -> 10 [line10]
  2  tx.line10.hold         -                                        -> 2 | tz [line10.hold bit]
  3  tx.bit                 TX=d, shift 0, K-=1, T=519               -> 4 [bit.hold]
  4  tx.bit.hold            -                                        -> 4 | tz,kz [bit.hold bit bit.hold stop]
  5  tx.bit                 TX=d, shift 0, K-=1, T=519               -> 4 [bit.hold]
  6  tx.bit.hold            -                                        -> 4 | tz,kz [bit.hold bit bit.hold stop]
  7  tx.stop                TX=1, T=519                              -> 8 [stop.hold]
  8  tx.stop.hold           -                                        -> 8 | tz [stop.hold idle]
  9  tx.idle                TX=1                                     -> 0 | rx0 [idle line9]
 10  tx.line10              TX=0, T=519                              -> 2 [line10.hold]
```

1. The columns are the address, `machine.state`, the operations, and `-> base | cA,cB` with the successors in
   address order: neither, `cA` only, `cB` only, both.
2. A state is named by its label, or `lineN` after its source line, with `.hold`, `.pad` or `.load` for the
   states a `hold` adds. A `ret` line shows `-> link`.
3. "Unused" counts the holes block alignment leaves.
4. The data bit prints as the program wrote it: `d` for the shifter and `c` for the LFSR, in pin operations
   (`TX=c`) and in conditions. An input prints as its pin name, as a condition (`RX`) and as the serial input
   (`shift RX`, `crc+=RX`). A compare prints its byte in decimal (`S==213`).

## 6. Limits and error messages

Every message but two has the form `file:line: message` and is followed by the source line, indented: for a
state line, the part after its label. The two about the whole file, `no machine` and `the table is full`, have
no line: `file: message`. For a file `prog.fsm` whose third line is `X=1, K+=1`:

```
prog.fsm:3: K and J only count down
    X=1, K+=1
```

A line inside a macro reports the line of its `use`, and shows the line as the macro pasted it.

| Message | Cause |
|---|---|
| `` expected `machine NAME` first `` | A state or pin line comes before any `machine` |
| `at most 4 machines` | A fifth `machine` |
| `no machine` | The file has no `machine` line |
| `machine M has no states` | A machine's section has no state line: the message names its `machine` line |
| `` expected NAME=P0..P7, got `X` `` | An `in` or `out` item is not `NAME=Pn` with n from 0 to 7 |
| `X is a reserved word` | A pin or a constant named with a reserved word |
| `` `in` pin X is declared twice `` (or `out`) | One name twice among a machine's inputs, or among its outputs |
| `` a machine has at most 3 `in` pins `` (or 4 `out` pins) | Too many inputs or outputs |
| `order is lsb or msb` | Anything else after `order` |
| `order is set twice: ...`, `crc init is set twice: ...` (or `poly`, `match`) | A setting given twice in one machine, even with the same value |
| `` `X` is a reserved word or a pin, not a label `` | A label with a reserved word (in a macro too) or a pin's name |
| `label L is defined twice` | Two labels with one name in one machine |
| `label L has no line after it` | A label at the end of a file or of a machine's section: the message names the label's line |
| `` expected `const NAME = expr` `` | A `const` line with no `=`, or no name before it (a name does not start with a digit) |
| `` cannot evaluate `E`: ... `` | An expression with an unknown name, `**`, a call other than `round`, `int`, `min` and `max`, a division by zero, a number too big (`1e400`, or anything over 1,024 bits), or words left over after a value (`crc poly=1 x`) |
| `crc NAME = V is outside 0 to 0xFFFFFFFF, the LFSR's 32 bits` | A negative or too large `poly`, `init` or `match`. Write -1 as `0xFFFFFFFF`. |
| `T=V does not fit its field, 0 to 65535`, `S==V does not fit its field, 0 to 255` | `T=` or `S==` out of range |
| `K holds 0 to 15`, `J holds 0 to 15` | `K=` or `J=` out of range |
| `the bus carries one byte` | A byte constant outside 0 to 255 |
| `` expected `macro NAME(P, Q)` `` | A `macro` line of any other form |
| `` macro M has no `end` `` | The file ends, or the next `macro` starts, before the `end` |
| `macro M is defined twice` | Two macros with one name |
| `macro M: each parameter is a name, and no two are the same` | A parameter that is a number or words, or is repeated |
| `no macro named M`, `M takes n arguments`, `macros nest too deep` | Mistakes in a `use`; nesting is limited to 8 levels |
| `` cannot read `X` `` | An action the language does not have, including `goto` on a line with other actions |
| `` unknown register or pin `X` `` | The left of `=` is not a pin, `T`, `K`, `J`, `crc` or a bus destination |
| `` `X` is an `in` pin; declare it with `out` as well to drive it `` | An input on the left of `=` |
| `a pin is set to one of: 0, 1, z, toggle, d, !d, od, c, !c` | Any other pin value |
| `a state changes two pins at most` | A third pin operation |
| `pin operations are gated by one condition` | `when A & B`, or two different `when` conditions |
| `pin operations in one state are all gated by the same condition, or none is` | Some pin operations have `when` and some do not |
| `` only pin operations can be gated with `when` `` | `when` after a register's assignment |
| `` unknown condition `X` `` | A guard names something that is not a condition |
| `` `edge` watches the machine's first `in` pin, and none is declared `` | `edge` with no `in` line above it |
| `a state tests two conditions at most; this one needs n` | More than two different conditions, counting `when` and `wait GUARD` |
| `` the rules of a `hold` line test one condition besides the timer's `tz`; these test n `` | Rules with two or more conditions on a `hold` line that waits for the timer |
| `one byte moves on the bus per state` | Two bus moves, or a bus move with `call`, `pop0` or `pop3` |
| `S is shifted or loaded in one state, not both` | `shift` with a write to S |
| `S shifts once per state` | `shift` twice |
| `one LFSR operation per state` | Two `crc` operations |
| `crc=0 clears the LFSR, crc=init loads its configured start value` | `crc=` with anything else |
| `` the serial input is 0, 1 or one of the first two `in` pins, not `X` `` | `shift C`, `crc+=C` or another name |
| `one state has one serial input` | Two different serial inputs, for example `shift` (a 0) with `crc+=RX` |
| `` `d` (shifter) and `c` (LFSR) cannot both be the data bit of one state `` | `d` and `c` together |
| `K and J only count down` | `K+=1` or `J+=1` |
| `K changes once per state`, `J changes once per state`, `T is loaded once per state` | Two changes of one register |
| `K and J cannot both be loaded in one state` | `K=` with `J=` |
| `N changes once per state` | Two counts of N |
| `N is loaded or counted in one state, not both` | `N=` with `N-=1` or `N+=1`: the load would replace the count |
| `` `ret` and a change of N cannot share a line: ... change N on the line before the `ret` `` | `N-=1` or `N+=1` with `ret`, in either order: the entry's N field holds the return code |
| `A and B set the same bits of the one immediate differently; put them on separate lines` | `T=` with `K=`, `J=` or a byte whose bits differ, or two different byte constants (`S==` counts as one) |
| `a waiting state repeats its actions every clock, so it may only set pins` | `wait` or `halt` with anything but pin operations, or with `toggle` |
| `` one `wait` or `halt` per line; join the conditions of a `wait` with `&` `` | Two of them on one line |
| `` `hold` and `wait` do not go together `` (or `halt`) | Both on one line |
| `` one `hold` per line `` | Two on one line |
| `hold needs at least one clock` | `hold 0` |
| `` `hold` lasts 65537 turns at most, all that the 16-bit timer counts `` | A longer `hold` |
| `` `hold` uses the timer; this state already loads T `` | `T=` on a `hold` line |
| `` test `S==` on a line of its own, not on a `hold` line `` | `S==` on a `hold` line, in a rule or a `when` |
| `` `call` and `ret` cannot share a line `` | Both on one line |
| `` `call` and `ret` stand alone: no rules, `wait`, `halt` or `hold` on their line `` | As it says |
| `` a machine cannot start with `goto`, which takes no turn; write `nop => L` `` | A `goto` above a machine's first state |
| `a call uses the immediate for its return address; move X to another line` | `call` with `T=`, `K=`, `J=`, a byte constant or `S==` |
| `nothing follows this call to return to` | A `call` on a machine's last line |
| `nothing follows this line` | A machine's last state line can fall through: give it a rule, `wait` or `halt` |
| `` no label `L` in machine M `` | A rule or a `call` names a label that is not in this machine |
| `machine M: goto L leads nowhere` | A `goto` to an unknown label, or a loop of `goto`s: the message names the `goto`'s line |
| `` `=>` needs rules ``, `` bad rule `R` `` | `=>` with nothing after it, or a rule whose target is not one name |
| `the table is full: more than 512 entries` | The program and its copies do not fit |

The compiler also prints `warning: M.S (line n) is never reached` for a state no machine can reach; it is left
out of the table.

## 7. Compile, run on the model, load into the chip

Everything runs on the system Python 3.9 or later with the standard library, from the repository's root.

### 7.1 Compile

Print a program's listing:

```
python3 -m bakeoff.cand_table.compiler docs/examples/uart_8e1_tx.fsm
```

It prints the summary line and the listing of section 5.1, and exits with status 0. On an error it prints the
message of section 6 to standard error, `file:line: message` and the source line, and exits with status 1.
Given several files, it compiles each one and exits with status 1 if any failed. A file it cannot read is
`file: reason`. Source files are UTF-8, with or without a byte-order mark; other bytes are harmless in comments.

Write the image to a file, for a host to load:

```
python3 -c "import sys; from bakeoff.cand_table.compiler import compile_file; sys.stdout.buffer.write(compile_file(sys.argv[1]).image)" docs/examples/uart_8e1_tx.fsm > uart_8e1_tx.bin
```

From Python, `compile_file(path)` and `compile_text(text, name)` in
[compiler.py](../bakeoff/cand_table/compiler.py) return a result with `.image` (bytes), `.listing` (the lines
above), `.states`, `.entries`, `.unused` and `.unreached`. They raise `CompileError`: `str()` of it is the
message of section 6, and its parts are `.filename`, `.line` (None for a message with no line), `.message` and
`.source` (the source line, or empty).

### 7.2 Run on the model

[bakeoff/harness/sim.py](../bakeoff/harness/sim.py) runs a chip model inside a model of the pads and the host.
The pieces a test script needs:

| Call | What it does |
|---|---|
| `Sim(TableChip(), image, pull=0xFF)` | Reset the model and load the image. Pins nobody drives sit at their `pull` level: all high by default. |
| `sim.set(pin, level)`, `sim.release(pin)` | The test drives a pin, or stops driving it. A pin is the wired-AND of everyone driving it. |
| `sim.send(ch, data)` | Queue bytes for channel 0 or 3. The host moves one byte per channel every 16 clocks. |
| `sim.received(ch)` | The bytes the host has read from channel 1 or 2 so far |
| `sim.tick()`, `sim.run(n)`, `sim.run_until(done, limit)` | Advance one clock, n clocks, or until `done(sim)` is true |
| `sim.wave`, `sim.oe_wave` | The level of every pin on every clock, and which pins the chip drove |
| `sim.peek()` | The pin levels the next tick will show. A model of the far end that must answer a pin in the same clock calls it, then `set` or `release`, then `tick`. |
| `sim.contention` | Pin-clocks where the chip and the test drove opposite levels |
| `sim.chip.mach[m]` | Machine m's registers: `state` (the entry address, as in the listing), `T`, `N`, `K`, `J`, `S`, `R`, `crc` |
| `edges(wave, pin)` | Every change of a pin as (clock, level) |

`TableChip` is in [bakeoff/cand_table/chip.py](../bakeoff/cand_table/chip.py) and `edges` in `sim.py`. The model
reads a pin two clocks after the pad changed and drives a pad one clock after the step, as the chip does. Two
differences from the chip: the model loads the image at once, and its synchroniser starts at the pull level, so
the first turn after the load sees every input at its pull level.

[docs/examples/run_uart_8e1.py](examples/run_uart_8e1.py) is a complete script: it drives a receiver's pin,
sends bytes to a transmitter, decodes the pins with its own decoder, and checks the channels.

### 7.3 Load into the chip

The demo board's RP2350 loads an image through the host pins:

1. Hold `rst_n` low for a few clocks, then release it.
2. Select port 2 (`ui_in[5:4]` = 2) and wait four clocks.
3. Write the image one byte at a time, low nibble first, one strobe edge per nibble, waiting for ROOM
   (`uo_out[4]`) before each byte.
4. Wait for RUNNING (`uo_out[6]`). The machines start on the next clock.

[info.md](info.md#the-host-link) gives the pin-level timing of a byte, and
[info.md](info.md#loading-a-program) the loader's rules: a new program needs a reset, and the chip loses its
program when it is deselected. The `Host` class in [test/test.py](../test/test.py) (lines 51 to 117) does all of
this against the RTL, and is the reference for host firmware.
