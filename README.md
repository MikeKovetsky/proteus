# protoemu

A programmable protocol-emulator chip for Tiny Tapeout's IHP shuttle, built for Jane Street's
[protocol-emulator ASIC competition](https://blog.janestreet.com/protocol-emulator-asic-competition/). AI agents
choose its instruction set in a simulator bake-off, then build the RTL against tests they cannot edit.

The chip is a table-driven state machine: a program is a state table, compiled from a small state-machine
language and held in two IHP SRAMs, and four machines can run protocols at once. It passes Tiny Tapeout's whole
flow at 8x4 tiles: layout, precheck and gate-level simulation, with timing closed at 60 MHz.

1. [docs/architecture.md](docs/architecture.md) explains the machine and why it is built this way,
   [docs/isa.md](docs/isa.md) is the reference for the table entry and the state-machine language, and
   [docs/programming.md](docs/programming.md) writes a new protocol step by step.
2. [src/](src/) holds the RTL, and [docs/info.md](docs/info.md) its pins, host link and loader.
3. [test/](test/README.md) holds the tests: through the pins, and clock for clock against the golden model.
4. [bakeoff/](bakeoff/README.md) holds the architecture bake-off: 14 protocol benches and three candidate
   architectures as Python simulators. The table-driven one, [bakeoff/cand_table](bakeoff/cand_table/README.md),
   is the golden model, with its compiler and programs.
5. [CLAUDE.md](CLAUDE.md) holds the requirements, this week's plan by day, the progress so far and every tool it
   uses.

Licensed under Apache 2.0, the same license as the Tiny Tapeout template.
