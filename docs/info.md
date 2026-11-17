## How it works

protoemu will be a small programmable chip that speaks UART, SPI, I2C and other protocols from programs loaded
after reset. It is an entry for Jane Street's protocol-emulator ASIC competition.

The architecture is still being chosen in a simulator bake-off. Until it is, `src/project.v` holds Tiny Tapeout's
template example (`uo_out = ui_in + uio_in`), kept only to prove the build at 8x4 tiles on IHP SG13CMOS5L.

## How to test

Run the cocotb test of the placeholder design:

```sh
cd test
make -B
```

It checks `uo_out = (ui_in + uio_in) mod 256` on six chosen pairs, which drive every output bit low and high and
carry through every bit, and then on 100 random pairs. It also checks that the design never drives the
bidirectional pins (`uio_oe = 0` and `uio_out = 0`).

## External hardware

None yet. The pinout in `info.yaml` names the placeholder adder's pins (A, B and SUM). It changes when the
architecture is chosen.
