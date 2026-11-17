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

It drives `ui_in = 20` and `uio_in = 30` and expects `uo_out = 50`.

## External hardware

None yet. The pinout will be filled in when the architecture is chosen.
