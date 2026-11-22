# Tests

All targets run from this folder and need the [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build)
on the path. `docs/info.md`, "How to test", says what each one checks.

| Command | What it runs | Time |
|---|---|---|
| `make -B` | 27 tests of the whole chip through its pins (`test.py`, `test_scenarios.py`), on Icarus | about 45 s |
| `make -B COCOTB_TEST_MODULES=known_gaps` | 17 known gaps, one test each. All fail until they are closed | about 40 s |
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
| `test_scenarios.py` | The 14 scenarios of `bakeoff/README.md`, the CRC references and three more I2C bus speeds |
| `known_gaps.py` | What the shipped programs get wrong or cannot do, not in the default run |

1. **Only pins.** `test_scenarios.py` and `bench.py` use top-level ports only. `test.py` also reads the SRAMs'
   `A_DLY` ties, which it finds by macro in the RTL and in a flat netlist.
2. **Separate judges.** `protocols/` imports the standard library and itself, never the bake-off harness, the
   golden model or the compiler. The compiler is used only to build the images the tests load.
3. **The clock.** It is 16,666 ps, 60.0024 MHz, the nearest period Icarus can toggle in whole picoseconds. (It
   was 16 ns, 62.5 MHz, which would make every absolute time 4% wrong.) `bench.py` counts one clock as exactly
   16.6667 ns, so a limit in ns or ppm is the limit of a chip at 60 MHz.
4. **I2C speeds.** The shipped program runs at about 350 kHz. The 400 and 100 kHz tests compile it with only
   `LOW` and `HIGH` replaced (80 and 62, 310 and 285). No shipped image runs at either speed.
5. **Ethernet receive runs below the standard's jitter.** Its test uses 3.5 ns, a transmitter's. A receiver must
   take 13.5 ns and this program loses frames from 8.5 ns (`gap_eth10_rx_jitter`).

Where the limits come from:

1. The CRC parameters, check values and codewords are from the
   [RevEng catalogue](https://reveng.sourceforge.io/crc-catalogue/all.htm). `zlib` is a second CRC-32.
2. The timing limits name their standard in each judge, but were typed from memory and cross-checked against
   vendor data sheets and quotations of the standards, not against the standards themselves.
3. These were not confirmed at all: Ethernet's 3.5 ns transmit jitter (a TI note says 5.5 ns), I2C's tLOW, tHIGH
   and tSU;STO, PS/2's 5 us before a falling clock, and USB's 14 ns paired source jitter (a TI data sheet says
   10 ns). A person should check them against the standards.
4. SPI has no standard, so its limits are the scenario's, or stricter.

A gap test states what should happen, so it fails today. When one passes, move it into `test_scenarios.py`.

## Gate-level simulation

CI's GDS workflow copies the routed netlist to `gate_level_netlist.v` and runs `make GATES=yes`. Before layout,
the same tests can run on the netlist Yosys maps to IHP cells:

1. Run `make synth`. It writes `synth/build/ihp_netlist.v`.
2. Copy it to `gate_level_netlist.v` here.
3. Run `make -B GATES=yes PDK_ROOT=<a folder holding ihp-sg13cmos5l/libs.ref>`. The tests need the PDK's
   `sg13cmos5l_stdcell.v`, `sg13cmos5l_udp.v` and `sg13cmos5l_io.v`.

All 27 tests are written to run there. They pass on a flat generic-gate netlist, with zero-delay cells (6.3
minutes) and with 0.08 ns gates and 0.6 ns flops (6.6 minutes). They have not yet run on the routed netlist or
under the cocotb 2.0.1 that CI installs. Scaled from CI's earlier 7-test run, gl_test should take about 10 minutes.

The SRAM model in `ihp_sram/` is IHP's own (its README says where it comes from), and both runs use it.

## Waveforms

`tb.v` writes `tb.fst`. Open it with `gtkwave tb.fst tb.gtkw` or `surfer tb.fst`.
