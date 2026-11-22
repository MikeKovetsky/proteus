# Tests

All targets run from this folder and need the [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build)
on the path. `docs/info.md`, "How to test", says what each one checks.

| Command | What it runs | Time |
|---|---|---|
| `make -B` | The cocotb tests of the whole chip through its pins (`test.py`), on Icarus | about 2 s |
| `make lockstep` | The golden model against the RTL core, clock for clock (`lockstep/`), on Verilator | about 10 s |
| `make mutants` | 21 planted bugs, one at a time; the lockstep suite must catch each | about 40 s |
| `python3 -m unittest discover -s flow` | The layout flow's own scripts on synthetic databases and reports (`flow/`): the SRAM pin buffers and diodes, the config they rely on, and CI's layout report | 1 s |
| `make lint` | Verilator lint of `src/`, every warning on | 1 s |
| `make synth` | Yosys: generic cells, IHP cells and the decision-loop delay (`synth/`) | a few seconds; fetches 1.8 MB once |

`make lockstep RANDOM=1000 SEED=7` runs more random tests, or other ones. A lockstep failure names the trace, the
clock (step), the signal, and what the model and the RTL gave.

## Gate-level simulation

CI's GDS workflow copies the routed netlist to `gate_level_netlist.v` and runs `make GATES=yes`. Before layout,
the same tests can run on the netlist Yosys maps to IHP cells:

1. Run `make synth`. It writes `synth/build/ihp_netlist.v`.
2. Copy it to `gate_level_netlist.v` here.
3. Run `make -B GATES=yes PDK_ROOT=<a folder holding ihp-sg13cmos5l/libs.ref>`. The tests need the PDK's
   `sg13cmos5l_stdcell.v`, `sg13cmos5l_udp.v` and `sg13cmos5l_io.v`.

The SRAM model in `ihp_sram/` is IHP's own (its README says where it comes from), and both runs use it.

## Waveforms

`tb.v` writes `tb.fst`. Open it with `gtkwave tb.fst tb.gtkw` or `surfer tb.fst`.
