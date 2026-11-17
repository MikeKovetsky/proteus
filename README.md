# protoemu

A programmable protocol-emulator chip for Tiny Tapeout's IHP shuttle, built for Jane Street's
[protocol-emulator ASIC competition](https://blog.janestreet.com/protocol-emulator-asic-competition/). AI agents
choose its instruction set in a simulator bake-off, then build the RTL against tests they cannot edit.

The only RTL so far is Tiny Tapeout's placeholder adder, in the IHP template at 8x4 tiles. [CLAUDE.md](CLAUDE.md)
holds the requirements, this week's plan by day and every tool it uses.

Licensed under Apache 2.0, the same license as the Tiny Tapeout template.
