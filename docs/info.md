## How it works

protoemu is a programmable protocol emulator: a state table in SRAM that up to four small machines walk, one entry
per clock, driving and sampling eight protocol pins. A program written in a state-machine language is compiled to
a table image and loaded through the host pins after reset. It is an entry for Jane Street's protocol-emulator
ASIC competition. The architecture, its instruction set and its compiler are described in
`bakeoff/cand_table/README.md`; the cycle-accurate Python model in `bakeoff/cand_table/chip.py` is the golden
model the RTL is tested against.

### The machine

1. **The table.** 512 entries of 64 bits, in two IHP `RM_IHPSG13_1P_512x32_c2_bm_bist` SRAM macros side by side
   (`src/protoemu_table.v`). One entry is one state.
2. **One decision per clock.** The SRAM's address register is the state register. The entry on the SRAM outputs
   picks two of 16 conditions and ORs them into its `base` address, and that address is latched at the end of the
   same clock. A return takes the machine's link register instead.
3. **Up to eight register operations in that same clock**: two pin operations, one byte move, a shift, an LFSR
   step, and counter and timer updates. Pin operations alone may be gated by the first condition.
4. **Four machines take turns** on the one SRAM port, each with its own registers (timer T, counter N, K, J,
   shifter S, R, a 32-bit LFSR with its polynomial, start and match values, a link register and its pin map).
   The datapath logic is shared (`src/protoemu_core.v`).
5. **Four 4-byte FIFOs** connect the machines to the host: channels 0 and 3 from the host, 1 and 2 to it
   (`src/protoemu_fifo.v`). A machine that pushes into a full FIFO drops the byte and sets the overflow flag.

### Pins

| Pins | Use |
|---|---|
| `uio[7:0]` | P0 to P7, the protocol pins. Programs map their inputs and outputs onto them. |
| `ui_in[3:0]` | Host data in, one nibble at a time, low nibble first |
| `ui_in[5:4]` | Port: 0 writes channel 0 and reads channel 1; 1 writes channel 3 and reads channel 2; 2 is the loader; 3 does nothing |
| `ui_in[6]` | Write strobe: every edge, rising or falling, takes the nibble on `ui_in[3:0]` |
| `ui_in[7]` | Read strobe: every edge moves the nibble on `uo_out[3:0]` on by one |
| `uo_out[3:0]` | Host data out: the low nibble of the read channel's oldest byte, then its high nibble |
| `uo_out[4]` | ROOM: the selected write channel has room (port 2: the loader takes bytes) |
| `uo_out[5]` | HAS_BYTE: the selected read channel has a byte |
| `uo_out[6]` | RUNNING: a program is loaded and the machines step |
| `uo_out[7]` | OVERFLOW: a byte was dropped since reset (see below) |

Every input pin, the protocol pins included, goes through a two-flop synchroniser. A pin the chip reacts to at
once shows its answer 3 clocks later: two in, one out through the pad register.

### The host link

1. **Writing a byte.** Set the port and the low nibble, wait one clock, toggle the write strobe, wait four clocks;
   then the same with the high nibble. The byte goes to the port's channel on the second edge. A nibble must be
   on the pins one clock before its edge and stay four clocks after it, so a byte takes about 10 clocks.
2. **Check ROOM first.** A byte written to a full channel is dropped and sets OVERFLOW.
3. **Reading a byte.** Wait for HAS_BYTE, read the low nibble on `uo_out[3:0]`, toggle the read strobe, wait four
   clocks, read the high nibble, toggle again. The second edge pops the byte. A pop only takes a byte the host was
   shown, so two edges on an empty channel do nothing.
4. **Whole bytes on one port.** The write and read nibble counters are shared by all ports and are cleared only
   by reset, so both nibbles of a byte go through the same port.
5. **Changing port.** The port is synchronised like the data. Wait four clocks after changing it before trusting
   ROOM, HAS_BYTE or the data nibble.
6. **Strobes low in reset.** The strobe synchronisers reset to 0, so a strobe held high through reset would count
   as an edge when reset ends.

### Loading a program

1. Hold `rst_n` low for a few clocks, then write the image to port 2, one byte at a time, as compiled by
   `python3 -m bakeoff.cand_table.compiler` (the format is in `bakeoff/cand_table/entry.py`): the machine count,
   16 configuration bytes per machine, the entry count (two bytes, low first), then 8 bytes per entry.
2. The loader writes each entry byte straight into its byte lane of the SRAMs, using the macros' bit mask. After
   the last entry it writes zeros to every entry above the count, up to 512 clocks, so no state the program can
   reach is left over from before.
3. RUNNING rises when the table is written, and the machines start on the next clock. Machine m starts in entry m.
4. **A new program needs a reset.** Bytes sent to the loader after RUNNING are ignored and set OVERFLOW.
5. The chip loses its program when it is deselected. Reselect, reset and load again.
6. Out-of-range images do something defined: a machine count above 4 runs 4 machines and skips the extra
   configuration bytes; an entry count above 512 loads 512 entries and ignores the rest. A count of 0 machines
   runs like 1, as in the model.

### OVERFLOW

It is set by a machine pushing into a full channel 1 or 2, by the host writing to a full channel 0 or 3, or by a
loader byte after the load. It stays set until reset.

## How to test

The default test runs the whole chip through its pins on Icarus (about 2 s, also on the gate-level netlist):

```sh
cd test
make -B
```

It loads compiled programs through the loader and checks, with its own decoders: `uart_tx` sending three bytes as
115200-baud frames on P0; `uart_rx` returning three frames from P1 to the host; `uart_3m` with two machines sending
and receiving at 3 Mbaud at once; port 1 with an echo program and the OVERFLOW bit; and reset in the middle of a
frame, reload, and running again with the same and another program.

The main evidence is the lockstep suite. It records the golden model running all 14 bake-off benches and 300 random
programs with random pins and random host traffic, replays every trace into the RTL core with Verilator, and
compares the pins, every host answer and the overflow flag on every clock (about 3 million clocks, 10 seconds):

```sh
make -C test lockstep
```

`make -C test mutants` plants 21 bugs in the RTL, one at a time, and checks that the lockstep suite catches each.
`make -C test lint` runs Verilator lint with every warning on, and `make -C test/synth` runs Yosys synthesis
(generic cells, then IHP cells at the slow corner) and estimates the delay of the one-clock decision loop.

## External hardware

None is needed beyond the demo board: its RP2350 drives the host pins. The protocol pins connect to whatever the
loaded program talks to (a UART, an SPI or I2C device, a PS/2 keyboard, WS2812 LEDs). Open-drain protocols such as
I2C and 1-Wire need pull-up resistors on their pins.
