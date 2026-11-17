# Architecture bake-off

Three candidate architectures for the chip, each written as a cycle-accurate Python simulator with an assembler
and a program for every scenario below. No RTL is involved. The table this produces is what Mike picks the
architecture from.

```bash
python3 -m bakeoff.run
```

```bash
python3 -m bakeoff.harness.selftest
```

Both run on the system Python 3.9 with the standard library only.

## The contract

1. A candidate lives in `bakeoff/cand_<name>/` and exposes `candidate.py` with three names:
   1. `NAME`, a short string.
   2. `make_chip()`, which returns a fresh `Chip` ([harness/chip.py](harness/chip.py)).
   3. `program(bench_name)`, which returns the assembled image for that scenario as `bytes`, or `None` if the
      candidate has no program for it.
2. One `Chip.step` is one clock at 60 MHz. The chip has eight protocol pins, P0 to P7, all bidirectional.
3. `Sim` ([harness/sim.py](harness/sim.py)) models the pads, so every candidate pays the same latency. A `step`
   sees the pins as they were two cycles earlier, and its outputs reach the pads one cycle later. A chip that
   reacts at once to a pin shows it three cycles (50 ns) later.
4. A pin is the wired-AND of its drivers: the chip (when `oe` is set), the bench, and otherwise the pull level.
   Opposite drivers count as contention, and contention fails a bench.
5. The host link is four byte FIFOs. Channel 0 carries what to send (host to chip), channel 1 what was received
   (chip to host), channel 2 status bytes (chip to host), and channel 3 is a spare (host to chip). The host moves
   at most one byte per channel every 16 cycles. FIFO depth is the candidate's choice and counts in its budget.
6. The chip does the protocol work. The host supplies only what a scenario lists: no framing, no CRC, no timing.

## Rules that keep the candidates honest

1. **One step is one 16.7 ns clock.** In one cycle, logic may be as deep as one 16-bit add or compare followed by
   a 4:1 mux. One step of a CRC up to 32 bits wide counts as one cycle. There is no multiplier and no divider.
2. **SRAM is synchronous.** The address goes in on one cycle and the data comes out on the next, one port per
   memory. The total is at most 32 kbit (two 512×32 macros).
3. **The flop budget is about 3,000 bits,** FIFOs and configuration included. The whole chip has roughly 32,000
   logic cells, and the host link, the loader, clock-tree buffers and routing need their share.
4. **No block may know a protocol by name.** A CRC unit with a programmable polynomial is fine. A "USB engine" is
   not. Each helper should serve at least two scenarios, or the README says why it is there.
5. **The budget is itemised** in the candidate's README and returned by `Chip.budget()`: `flops`, `sram_bits`,
   `gates_estimate`, `engines` (how many protocols can run at once) and `notes`.
6. **Programs are readable sources** under `programs/`, built by the candidate's own assembler or compiler. How
   easy a new protocol is to write is part of what is being compared.

## Scenarios

Times are given in clock cycles at 60 MHz where it helps. Payloads come from `harness.sim.payload`, so every
candidate gets the same bytes. Pins not named are unused and pulled high.

### UART

1. **`uart_tx`** P0 = TX. 8N1, 115200 baud (520.83 cycles per bit), idle high, LSB first. Channel 0 gets 16
   bytes, and the chip sends each one. It passes if all 16 decode in order and every edge inside a frame is
   within 2% of a whole number of bit times from that frame's start edge.
2. **`uart_rx`** P1 = RX. The bench sends 16 bytes back to back at 115200 baud, once 2% fast and once 2% slow.
   The chip pushes each byte to channel 1. Both runs must match.
3. **`uart_3m`** P0 = TX, P1 = RX, 3 Mbaud (20 cycles per bit), 8N1, both directions at once. Channel 0 gets 16
   bytes to send while the bench sends 16 others, 2% fast. Both directions must match.

### SPI

Pins for all three: P0 = SCK, P1 = MOSI, P2 = MISO, P3 = CS (active low). Bytes go MSB first.

4. **`spi_ctrl_m0`** The chip is the controller in mode 0 (clock idles low, both sides sample on the rising
   edge). SCK is 5 MHz, a period of 12 cycles, give or take one. Channel 0 gets `[n][n bytes]`, with n at most
   16. The chip pulls CS low at least 6 cycles before the first rising edge, transfers n bytes, raises CS, and
   pushes the n bytes it read to channel 1. The bench's target changes MISO on the falling edge with no delay, so
   the program has to allow for the pad latency when it samples.
5. **`spi_ctrl_m3`** The same in mode 3 (clock idles high, shift on the falling edge, sample on the rising edge).
6. **`spi_target_m0`** The chip is the target in mode 0, and P0, P1 and P3 are inputs. Channel 0 gets 8 bytes
   before CS falls. The bench clocks 8 bytes at 2 MHz (30 cycles per period), with CS low 60 cycles before the
   first rising edge. The chip returns the 8 bytes on MISO, each bit stable from 3 cycles before the rising edge
   to 1 cycle after it, and pushes the 8 bytes it read to channel 1. It must not drive MISO while CS is high.

### I2C

7. **`i2c_ctrl`** P0 = SCL, P1 = SDA, both open drain: the chip must never drive either one high. Fast mode: SCL
   low for at least 78 cycles, high for at least 36, a period of 150 to 200 cycles when nobody stretches. SDA
   changes only while SCL is low, except for START and STOP, with at least 6 cycles of setup. Channel 0 gets
   `[addr7][nw][nw bytes][nr]`. The chip sends START, the address with write, the nw bytes, a repeated START,
   the address with read, reads nr bytes (ACK on all but the last), and sends STOP. It pushes the nr bytes to
   channel 1, then one status byte to channel 2: `0x00` done, `0x01` NACK.
   1. Run A: the target ACKs, and once holds SCL low for 20 µs after an ACK bit. The chip must wait.
   2. Run B: nobody answers the address. The chip sends STOP, pushes nothing to channel 1, and reports `0x01`.

### WS2812

8. **`ws2812_tx`** P0 = data out, idle low. Channel 0 gets `[n][3n bytes]`, n at most 8, each byte MSB first. A
   0 bit is high for 24 cycles then low for 51. A 1 bit is high for 48 cycles then low for 27. Each time may be
   off by 9 cycles. After the last bit the line stays low for at least 3,000 cycles.

### 1-Wire

9. **`onewire`** P0 = DQ, open drain. The chip is the controller at standard speed.
   1. Reset: low for 480 to 960 µs, then release. A device answers by pulling low from 30 µs after the release,
      for 120 µs.
   2. Write the byte `0x33` LSB first. A 1 is low for 1 to 15 µs. A 0 is low for 60 to 120 µs. Slots start at
      least 61 µs apart, with at least 1 µs high between them.
   3. Read 64 bits LSB first. For each, the chip pulls low for 1 to 15 µs and releases. The device holds the line
      low until 30 µs after the slot began to send a 0.
   4. The chip pushes the 8 bytes to channel 1, then to channel 2: `[0x00]` for present, then `[0x00]` if the
      CRC-8 of the 8 bytes is good (x⁸+x⁵+x⁴+1, reflected `0x8C`, checked on chip) or `[0x01]` if not.
   5. Run A: a device with a good CRC. Run B: a bad CRC. Run C: no device, where the chip pushes only `[0x01]`
      to channel 2 and stops.

### PS/2

10. **`ps2_rx`** P0 = CLK, P1 = DATA, both driven by the bench. The chip only listens. The clock is 12.5 kHz
    (4,800 cycles per period). A frame is a start bit (0), 8 data bits LSB first, odd parity and a stop bit (1),
    and data is valid on the falling clock edge. The bench sends 8 frames, one with wrong parity. The chip pushes
    each good byte to channel 1, and `0xFF` to channel 2 for the bad frame.

### Low-speed USB

Pins for both: P0 = D+, P1 = D−. The pulls make the idle state J (P1 high, P0 low). K is the opposite, and SE0
is both low. A bit is 40 cycles (1.5 Mbit/s).

11. **`usb_ls_tx`** Channel 0 gets `[n][PID][n−1 data bytes]`, n at most 9. The chip sends SYNC (`KJKJKJKK`), the
    PID, the data, and the CRC-16 of the data bytes (polynomial `0x8005`, reflected, initial value all ones,
    inverted, low byte first), computed on chip. Bytes go LSB first. It stuffs a 0 after six 1s, codes NRZI (a 0
    toggles the line), ends with SE0 for two bit times and J for one, and then stops driving. Every edge sits
    within 2 cycles of its place on a 40-cycle grid from the start of SYNC. Three packets: no data, eight `0xFF`
    bytes, and five payload bytes.
12. **`usb_ls_rx`** The bench sends four packets, and the chip never drives. Their bit rates are 1.5% fast, 1.5%
    slow, nominal and nominal, each starting at a random phase. The chip recovers the bit clock from the edges,
    undoes NRZI and the stuffing, and checks the CRC-16. For each packet it pushes every byte after SYNC (PID,
    data, CRC) to channel 1, then to channel 2: `[0x00 good or 0x01 bad CRC][byte count]`. The fourth packet has
    a corrupted CRC.

### 10 Mbit Ethernet

A bit is 6 cycles, and each half of a Manchester bit is 3. A 1 is low then high, and a 0 is high then low.
Bytes go LSB first.

13. **`eth10_tx`** P0 = TX+, P1 = TX−, both pulled low, and both low when idle. Channel 0 gets
    `[len low][len high][len bytes]`: the frame from destination address to the end of the payload, 60 to 300
    bytes, with no preamble and no FCS. The chip sends seven `0x55` bytes, `0xD5`, the frame and the FCS (CRC-32,
    polynomial `0x04C11DB7`, reflected, initial value all ones, inverted, low byte first), computed on chip.
    TX− is the complement of TX+ while sending. Every half bit is exactly 3 cycles. The frame ends with TX+ high
    and TX− low for 12 to 24 cycles, then idle. Two frames, 60 and 300 bytes, at least 576 cycles apart.
14. **`eth10_rx`** P2 = RX, driven by the bench, idle low. The bench sends three frames (64, 65 and 300 bytes
    with FCS) behind the usual preamble and `0xD5`. Its bit period is 6 cycles, off by up to 200 ppm, starting at
    a random phase, so some half bits arrive 2 or 4 cycles long. The chip locks onto the preamble, finds the
    `0xD5`, decodes, and pushes every byte after it (FCS included) to channel 1. At the end of each frame it
    pushes to channel 2: `[0x00 good or 0x01 bad FCS][len low][len high]`. The third frame has one flipped bit.

## What each candidate hands in

1. `candidate.py`, the simulator, the assembler or compiler, and the program sources under `programs/`.
2. A `README.md` of at most 200 lines: the machine and its instruction set, the itemised budget, how many
   protocols run at once, how the pads and the host link map onto Tiny Tapeout's 24 pins, and what the candidate
   can do that a PIO cannot.
3. A plain list of the scenarios it fails, each with the reason: a limit of the architecture, or a lack of time.
