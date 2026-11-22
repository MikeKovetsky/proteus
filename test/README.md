# Tests

All targets run from this folder and need the [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build)
on the path. `docs/info.md`, "How to test", says what each one checks.

| Command | What it runs | Time |
|---|---|---|
| `make -B` | 74 tests of the whole chip through its pins (`test.py`, `test_scenarios.py`, `test_programs.py`), on Icarus | about 2 min |
| `make -B COCOTB_TEST_MODULES=known_gaps` | 10 known gaps, one test each. All fail until they are closed | about 40 s |
| `make lockstep` | The golden model against the RTL core, clock for clock (`lockstep/`), on Verilator | about 10 s |
| `make mutants` | 21 planted bugs, one at a time; the lockstep suite must catch each | about 40 s |
| `make lint` | Verilator lint of `src/`, every warning on | 1 s |
| `make synth` | Yosys: generic cells, IHP cells and the decision-loop delay (`synth/`) | a few seconds; fetches 1.8 MB once |

`make lockstep RANDOM=1000 SEED=7` runs more random tests, or other ones. A lockstep failure names the trace, the
clock (step), the signal, and what the model and the RTL gave.

## The pin tests

`make` can succeed when a test fails, so check the results as CI does:

```sh
make -B
python3 -m cocotb_tools.check_results results.xml
```

| File | What it holds |
|---|---|
| `host_link.py` | The host's side of the pins: clock, reset, nibble link and loader |
| `bench.py` | The board round `uio` (pulls, open drain, rise times, contention) and the host's firmware |
| `protocols/` | One judge per protocol: decoder, far end and limits, with the standard named at the top |
| `test.py` | The host link, loader, reset, overflow and pad latency, counted in clocks |
| `test_scenarios.py` | The 14 scenarios of `bakeoff/README.md`, the CRC references and four more I2C runs (edges, speeds, a late host) |
| `test_programs.py` | The other rates and modes, the gaps that closed, and the errors the programs report. No scenario's expectation |
| `known_gaps.py` | What the shipped programs get wrong or cannot do, not in the default run |

1. **Only pins.** `test_scenarios.py` and `bench.py` use top-level ports only. `test.py` also reads the SRAMs'
   `A_DLY` ties, which it finds by macro in the RTL and in a flat netlist.
2. **Separate judges.** `protocols/` imports the standard library and itself, never the bake-off harness, the
   golden model or the compiler. The compiler is used only to build the images the tests load.
3. **The clock.** It is 16,666 ps, 60.0024 MHz, the nearest period Icarus can toggle in whole picoseconds. (It
   was 16 ns, 62.5 MHz, which would make every absolute time 4% wrong.) `bench.py` counts one clock as exactly
   16.6667 ns, so a limit in ns or ppm is the limit of a chip at 60 MHz.
4. **I2C speeds and time-out.** `test_scenarios.py` retimes the program's two constants to 400 and 100 kHz (80
   and 62, 310 and 285). `test_programs.py` runs `i2c_ctrl` and `i2c_ctrl_100k` as shipped at both speeds, up to
   each mode's slowest edges. One test waits out the shipped 25 ms time-out, about 25 seconds of real time. The
   test of every phase that can time out compiles the program with that shortened to 1.1 ms.
5. **Ethernet receive runs below the standard's jitter.** Its test uses 5.5 ns, the most a whole sending station
   may add. A receiver must take 13.5 ns and this program loses frames from 8.5 ns (`gap_eth10_rx_jitter`).
6. **Driven, not pulled.** A pin nobody drives reads as its pull, so the transmit tests also check that the chip
   drives each push-pull pin (`Bench.driven`).

### Where the limits come from

An AI reviewer checked each limit against these on 2026-11-21, reading text copies (some scanned), not the
publishers' PDFs. Each constant in `protocols/` names its row, or says that it is the scenario's or the test's own.

1. **CRCs.** The [RevEng catalogue](https://reveng.sourceforge.io/crc-catalogue/all.htm) has the parameters, check
   values and codewords. `zlib` is a second CRC-32.
2. **I2C.** NXP's UM10204 revision 7.0, table 11 and its notes
   ([text copy](https://github.com/zhengboon/EE2028Assign2/blob/main/datasheets/UM10204%20I2C-bus%20specification%20and%20user%20manual.txt)).
   The same figures are in
   [Linux's timing table](https://github.com/torvalds/linux/blob/master/drivers/i2c/busses/i2c-rk3x.c), and the
   300 ns hold is in [NXP's PCA9685 data sheet](https://github.com/thepinkmile/Enigma-NG/blob/main/design/Datasheets/PCA9685-datasheet.md), note 4.
3. **Low-speed USB.** The [USB 2.0 specification](https://archive.org/download/USB-2.0/Universal%20Serial%20Bus%20Specification%2C%20Revision%202.0_djvu.txt),
   tables 7-10 and 8-1, and sections 7.1.7 to 7.1.18.
4. **10BASE-T.** Two secondary sources:
   [Cirrus's CS8900A](https://archive.org/details/bitsavers_crystalSemsCS8900Ads200401_1850904) and
   [Level One's LXT901A](https://archive.org/details/bitsavers_levelOneetLXT901907200712_459592) data sheets.
   **IEEE 802.3 itself was not read**, so nothing here is confirmed against the standard's own text.
5. **PS/2.** [IBM's PS/2 Hardware Interface Technical Reference](https://archive.org/details/ps-2-hardware-interface-technical-reference-ocr),
   "Keyboard and Auxiliary Device Controller (Type 1)", figures 7 and 8.
6. **1-Wire.** The AC tables of Maxim's [DS18B20](https://archive.org/details/manuallib-id-2607118) and
   [DS2401](https://archive.org/details/manuallib-id-2600669) data sheets.
7. **WS2812B.** Worldsemi's [first data sheet](https://github.com/crimpdeq/crimpdeq-pcb/blob/main/datasheets/WS2812B.md)
   and [WS2812B-V5's](https://github.com/Averyy/pcbparts-mcp/blob/main/data/design-rules/rules_sources/misc/leds/worldsemi-ws2812b-v5.md).
8. **SPI has no standard.** The shape and the lead, trail and idle times are
   [Motorola's SPI Block Guide's](https://archive.org/details/manualzilla-id-6909510). The target's 50 ns answer is
   a [25LC256's](https://archive.org/details/manuallib-id-2628863). The rest is the scenario's or the test's own.
9. **UART has no standard here.** The baud rates and the 2% are the scenarios'.

These figures are the tests' own choices, so no source can confirm them: the 2 ns pad delay, SPI's MOSI margin and
100 ns MISO release, the I2C target's 10 ns margin and the stricter reading of tVD;DAT's note 3, the 1-Wire bus's
1 us rise and the device's 30 us sampling point, PS/2's 50 us between frames and its 2 ms resync gap, and counting
the Ethernet interframe gap from the end of TP_IDL.

A gap test states what should happen, so it fails today, and a chip that does nothing passes none of them. When one
passes, move it into `test_programs.py` with its body unchanged. A gap that contradicts a scenario's wording, as
`gap_i2c_plain_read` does, waits for Mike instead. `test_scenarios.py` and `protocols/` don't change for a program.

## Gate-level simulation

CI's GDS workflow copies the routed netlist to `gate_level_netlist.v` and runs `make GATES=yes`. Before layout,
the same tests can run on the netlist Yosys maps to IHP cells:

1. Run `make synth`. It writes `synth/build/ihp_netlist.v`.
2. Copy it to `gate_level_netlist.v` here.
3. Run `make -B GATES=yes PDK_ROOT=<a folder holding ihp-sg13cmos5l/libs.ref>`. The tests need the PDK's
   `sg13cmos5l_stdcell.v`, `sg13cmos5l_udp.v` and `sg13cmos5l_io.v`.

All the tests are written to run there. On 2026-11-22 an earlier 28 of them passed on that mapped netlist under
cocotb 2.1-dev, with stand-in cell models made from the liberty's logic functions: with zero delay, and with 0.08 ns
gates and 0.6 ns flops (17 and 18 minutes of CPU on a busy laptop). The programs have changed since. With a delayed
tie cell the SRAM model prints an `A_DLY` error at time 0 and runs on. That is the stand-in's doing.

They have **not** run on the routed netlist, with IHP's own cell models or under the cocotb 2.0.1 that CI installs.
At the 3,700 clocks a second of CI's last 7-test gl_test, those 28 tests' 2.2 million clocks would have taken it
about 10 minutes. The suite now simulates several times as long. Neither is a CI run.

The SRAM model in `ihp_sram/` is IHP's own (its README says where it comes from), and both runs use it.

## Waveforms

`tb.v` writes `tb.fst`. Open it with `gtkwave tb.fst tb.gtkw` or `surfer tb.fst`.
