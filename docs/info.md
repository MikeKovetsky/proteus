## How it works

protoemu is a programmable protocol emulator: a state table in SRAM that up to four small machines walk, one entry
per clock, driving and sampling eight protocol pins. A program written in a state-machine language is compiled to
a table image and loaded through the host pins after reset. It is an entry for Jane Street's protocol-emulator
ASIC competition. The cycle-accurate Python model in `bakeoff/cand_table/chip.py` is the golden model the RTL is
tested against.

1. [architecture.md](architecture.md) explains the machine, its datapath, its budget and its limits.
2. [isa.md](isa.md) is the programmer's reference: the table entry, the state-machine language, the compiler's
   messages, and how to run and load a program.
3. [programming.md](programming.md) writes a new protocol step by step and shows the patterns the bench programs
   use.

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
   clocks, read the high nibble, toggle again and wait four clocks. The second edge pops the byte; until it has,
   HAS_BYTE and `uo_out[3:0]` still show the byte just read. The second edge pops only when the channel holds a
   byte, so edges on an empty channel pop nothing.
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
   configuration bytes; an entry count above 512 loads the first 512 entries and starts, and the loader takes no
   more bytes (ROOM stays low, and a byte sent anyway sets OVERFLOW). A count of 0 machines runs like 1, as in
   the model. The compiler emits neither (at most 4 machines and 512 entries), and no test loads them.

### OVERFLOW

It is set by a machine pushing into a full channel 1 or 2, by the host writing to a full channel 0 or 3, or by a
loader byte after the load. It stays set until reset.

## The programs

`bakeoff/cand_table/programs/` has one source per image, and each file's header says what the host sends and what
it gets back on channel 1 (data) and channel 2 (status).

| Programs | What they do | Status on channel 2 |
|---|---|---|
| `uart_tx_9600`, `uart_tx`, `uart_tx_921600` | 8N1 transmit at 9600, 115200 and 921600 baud | None |
| `uart_rx_9600`, `uart_rx`, `uart_rx_921600` | 8N1 receive at the same three rates | `0x01` for a frame whose stop bit is 0 |
| `uart_3m` | 8N1 both ways at once at 3 Mbaud | `0x01` for a frame whose stop bit is 0 |
| `spi_ctrl_m0`, `_m1`, `_m2`, `_m3` | SPI controller in modes 0 to 3 at 5 MHz. The clock waits for a late host | None |
| `spi_target_m0` | SPI target in mode 0 | `0x01` for each byte clocked out with none queued (it is `0x00`) |
| `i2c_ctrl`, `i2c_ctrl_100k` | I2C controller at 400 and at 100 kHz: a write then a read, or a write alone when the read count is 0 | `0x00` done, `0x01` NACK, `0x02` bus stuck |
| `ws2812_tx` | WS2812B and its V5: 75 clocks a bit, 282 us of latch | `0x01` each time the host was late inside a frame |
| `onewire` | 1-Wire reset, presence and Read ROM | `0x00`, then `0x00` good CRC or `0x01` bad. Or only `0x01`: no device, or a short |
| `ps2_rx` | PS/2 from a device at 10 to 16.7 kHz | `0xFF` for a wrong start, parity or stop bit, or a clock that stops for 250 us inside a frame |
| `usb_ls_tx` | Low-speed USB data packets: every packet gets the CRC-16 of the bytes after its PID | `0x01` for a packet the host was late in. It goes out with a stuffing error |
| `usb_ls_rx` | Low-speed USB packets, judged by CRC-16, with a hub's extra bit before EOP let through | `0x00` good or `0x01` bad, then the byte count |
| `eth10_tx` | 10 Mbit Ethernet frames, with preamble and FCS. No link pulses | `0x01` for a frame the host was late in. It goes out cut short, with a wrong FCS |
| `eth10_rx` | 10 Mbit Ethernet receive, up to 8 ns of jitter | `0x00` good or `0x01` bad FCS, then the length, low byte first |

What to know before using them:

1. **UART rates** are one constant, `BIT` (60 MHz / baud). With one machine the 16-bit timer takes transmit down
   to 916 baud and receive down to 1,374; the compiler refuses a lower one. 921,600 baud is 65 clocks a bit,
   0.16% slow. Apart from `uart_3m` and the four-machine program, each UART image is one direction.
2. **The SPI target takes a byte from channel 0 early**, as the last bit of the byte before it is sampled, to
   have it ready for the next falling edge. A byte the host queues later waits for the byte after. A byte is kept
   when CS rises before its first clock, and dropped, in both directions, when CS rises inside it.
3. **I2C is a single controller** with 7-bit addresses. A read always follows address+W and a repeated START,
   even with nothing to write, as scenario 7 asks: it cannot read a target that will not take its write address.
   It gives up on an SCL held low for 25 ms. It also gives `0x02`, and does not clock the bus free, when SDA is
   low before START or at STOP. While it waits for the host's next byte in a write, it holds SCL low for as long
   as that takes.
4. **WS2812's timing has 3 ns to spare** on one limit: a 1 is low for 583 ns, the V5 wants 580 at least and the
   first WS2812B 600 at most. A slow output edge counts against that. The latch is 2 us over the V5's 280.
5. **`extra/four_at_once`** drops a UART byte whose stop bit is 0, and a bad PS/2 frame, without a report:
   channel 2 is PS/2's data there.
6. **USB is data packets only.** Both programs give every packet a CRC-16, as scenarios 11 and 12 say: transmit
   cannot send a handshake or a token, and receive reports both as bad. Receive also reports seven 1s as bad, and
   accepts six just before EOP, as the harness's decoder does. It takes its edges from D+ alone, and loses some
   packets when 75 ns of jitter between neighbouring edges comes with a long SE0 inside transitions: above 140 ns
   at 1.5% off in rate, and above 190 ns at the exact rate. Either one, at its limit and 1.5% off in rate, is
   received.
7. **A USB or Ethernet transmitter that was cut short waits for the rest** of that packet or frame from the host,
   and drops it, before it starts the next.

## How to test

The default test runs the whole chip through its 24 pins on Icarus: 103 tests in about 3 minutes. They are
written to run on a gate-level netlist too (`GATES=yes`). An earlier 28 of them have, on a synthesised netlist with
stand-in cell models; these programs have not, and nothing has yet run on the routed netlist or under CI's cocotb.

```sh
cd test
make -B
python3 -m cocotb_tools.check_results results.xml
```

The tests load compiled programs through the loader and talk to them through the host link. The 14 scenarios of
`bakeoff/README.md` are judged on the protocol pins, at 60 MHz, by decoders and far ends in `test/protocols/` that
were written from each protocol's definition and share no code with the golden model or the bake-off harness:

| Scenario | What the test requires |
|---|---|
| `uart_tx`, `uart_rx`, `uart_3m` | 8N1 at 115200 and 3 Mbaud, both ways at once at 3M, edges and senders within 2% |
| `spi_ctrl_m0`, `spi_ctrl_m3` | 5 MHz: period, phase, CS lead and trail, setup and hold; a target up to 50 ns late (modes 1 and 2 too) |
| `spi_target_m0` | A 2 MHz controller; MISO stable round the sampling edge and released with CS high |
| `i2c_ctrl` | UM10204's controller timing (`i2c_ctrl_100k` too), stretching, every form of request, each place a NACK can come |
| `ws2812_tx` | WS2812B's and the V5's pulse widths and latch, 50 and 280 us |
| `onewire` | Reset, presence at both ends of its range, slot timing, Read ROM with a good and a bad CRC-8, and shorts |
| `ps2_rx` | A device at 10, 12.5 and 16.7 kHz, and one bad parity |
| `usb_ls_tx`, `usb_ls_rx` | NRZI, stuffing, CRC-16, EOP and packet spacing; receive at 1.5% off with a device's and a host's allowed jitter |
| `eth10_tx`, `eth10_rx` | Manchester, TP_IDL, gap and FCS up to a full frame, in real time; receive at 200 ppm off with 5.5 ns of jitter |

The transmit tests also require each push-pull pin to be driven, because a pin left to its pull reads the same. One
test checks the judges' CRCs against the RevEng catalogue's check values and codewords. Four more run I2C on fast
mode's slowest edges, with a late host and, with sharp edges, retimed to 400 and 100 kHz. `test/test_programs.py`
holds the programs to the section above: the other UART rates and SPI modes, both I2C programs as shipped with the
sharpest and slowest edges and the time-out, the V5's WS2812 timing, a shorted 1-Wire bus, bad and abandoned PS/2
frames, bad stop bits, a hub's extra bit and stuffing errors on USB, queued bytes and late hosts. The seven in
`test/test.py` cover what the scenarios do not: the loader with reset and reload, both host ports, ROOM and
OVERFLOW, the pads' three clocks of round trip, and the SRAMs' `A_DLY` ties (IHP's model reports a wrong tie only
with a `$stop`, which Icarus runs past when nothing answers on its stdin, as in CI).

What the shipped programs do not yet do:

1. Ethernet receive takes 8 ns of jitter, not the standard's 13.5 ns, so its test uses 5.5 ns, the most a whole
   sending station may add (`gap_eth10_rx_jitter`). A program cannot guarantee 13.5 ns on this chip, which samples
   each pin every 16.7 ns: `test/eth10_jitter_limit.py` builds pairs of different good frames that give the same
   samples at 13.5, 10 and 8.4 ns of jitter. It takes finer sampling in the RTL, which is not built.
2. I2C has no read without address+W before it (`gap_i2c_plain_read`). Scenario 7 asks for the address+W, so
   changing it waits for Mike's decision.
3. USB has no handshakes or tokens (`gap_usb_tx_handshake`, `gap_usb_rx_handshake`). Scenarios 11 and 12 ask for
   a CRC-16 on every packet, so changing it waits for Mike's decision.
4. Ethernet transmit sends no link pulses (`gap_eth10_link_pulses`). Scenario 13 asks for both lines low when
   idle, so changing it waits for Mike's decision.
5. `test/known_gaps.py` has these gaps, 5 tests that fail until each is closed:
   `make -B COCOTB_TEST_MODULES=known_gaps`.

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
