## How it works

protoemu will be a small programmable chip that speaks UART, SPI, I2C and other protocols from programs loaded
after reset. It is an entry for Jane Street's protocol-emulator ASIC competition.

Until the real RTL lands, the tile holds a flow test (`src/flowtest_top.v`). It proves that Tiny Tapeout's flow
accepts, places, powers and simulates the two IHP 512 x 32 SRAM macros the chosen architecture keeps its table in.
The two macros form one 512 x 64 table. After reset a loader writes every entry with a pseudo-random pattern, reads
it back and reports the result. Then the table drives its own address for 1024 clocks, through the same loop the
real design uses, and every word read is folded into a signature and into about 1,000 filler flops.

## How to test

Run the cocotb tests of the flow test:

```sh
cd test
make -B
```

A Python model in `test/test_flowtest.py` computes the self-test result, the walk's signature and the last filler
lane. The tests check them on `uo_out` and `uio_out`, with the self-test passing, with 16 planted faults, and after
a reset in the middle of the walk.

## External hardware

None. Hold `ui_in[7:4]` from reset, wait for `uo_out[3]`, then step `ui_in[3:0]` through 0 to 15 to read the 16
result bytes on `uio_out`.
