# Tang Nano 9K Breadboard Wiring Guide

Run unmodified **Proteus RTL v2** directly off the onboard **27.000 MHz** crystal (zero PLL jitter, ~30.7 MHz post-PnR Fmax on GW1NR-9C C6/I5; 48% LUT4, 35% DFF, 8/26 BSRAM) on a **Sipeed Tang Nano 9K**, wired directly to a **Raspberry Pi Pico 2** host and real physical target ICs (**W25Q32 SPI Flash**, **LM75A I2C sensor**, and a physical **red `KILL` jumper wire**).

> *(On the target IHP 130 nm CMOS5L ASIC, Proteus closes full post-PnR timing at **60.0 MHz** with +3.55 ns slow-corner setup slack. On the budget 55 nm C6 FPGA fabric, running directly at 27 MHz gives ~+4 ns positive setup margin — protocol timing constants simply scale linearly by `27/60`.)*

> **Voltage Safety Rule (Important!)**
> All 24 Tiny Tapeout pins and `ext_rst_n` are assigned strictly to **3.3V Bank 1 / Bank 2** headers (`LVCMOS33`).
> **Do NOT connect 3.3V wires to Tang Nano 9K pins 79–86** (Bank 3 is 1.8V!).

---

## 1. Build & Flash the FPGA Bitstream

```bash
source ~/oss-cad-suite/environment
make -C fpga/tang_nano_9k bitstream   # synthesize + PnR + pack build/proteus_tang9k.fs
make -C fpga/tang_nano_9k sram        # instant volatile SRAM load via USB-C
# or:
make -C fpga/tang_nano_9k flash       # persistent flash load (boots automatically on power)
```

---

## 2. Power & Ground (Common Ground Required!)

| Tang Nano 9K | Breadboard Rail | Raspberry Pi Pico 2 | Target ICs (W25Q32 / LM75A) |
|---|---|---|---|
| `GND` | Blue (`-`) rail | `GND` (any pin, e.g. pin 38) | `GND` |
| `3V3` | Red (`+`) rail *(or power rail from Pico 2 `3V3(OUT)`)* | `3V3(OUT)` (pin 36) | `VCC` (3.3V) |

*(Tip: Power both Tang Nano 9K and Pico 2 over their own USB-C / micro-USB cables, connect their `GND` pins together on the breadboard `-` rail, and power the W25Q32 / LM75A from Pico 2's `3V3(OUT)` pin.)*

---

## 3. Host Nibble Link (Tang Nano 9K ↔ Raspberry Pi Pico 2)

| Tiny Tapeout Pin | Function | Tang Nano 9K Pin | Suggested Pico 2 GPIO |
|---|---|---|---|
| `ui_in[0]` | Host data in bit 0 | **33** | GP0 |
| `ui_in[1]` | Host data in bit 1 | **34** | GP1 |
| `ui_in[2]` | Host data in bit 2 | **35** | GP2 |
| `ui_in[3]` | Host data in bit 3 | **40** | GP3 |
| `ui_in[4]` | Host port select bit 0 | **41** | GP4 |
| `ui_in[5]` | Host port select bit 1 | **42** | GP5 |
| `ui_in[6]` | Host write strobe (`wr_edge`) | **48** | GP6 |
| `ui_in[7]` | Host read strobe (`rd_edge`) | **49** | GP7 |
| `uo_out[0]` | Host data out bit 0 | **51** | GP8 |
| `uo_out[1]` | Host data out bit 1 | **53** | GP9 |
| `uo_out[2]` | Host data out bit 2 | **54** | GP10 |
| `uo_out[3]` | Host data out bit 3 | **55** | GP11 |
| `uo_out[4]` | Status: `ROOM` | **56** | GP12 |
| `uo_out[5]` | Status: `HAS_BYTE` | **57** | GP13 |
| `uo_out[6]` | Status: `RUNNING` | **76** | GP14 |
| `uo_out[7]` | Status: `OVERFLOW` | **77** | GP15 |
| `ext_rst_n` | Active-low reset (optional, internal pull-up) | **63** | GP16 |

---

## 4. Protocol Pins `P0..P7` (Tang Nano 9K Contiguous Header Pins 25–32)

| Proteus Pin | Tang Nano 9K Pin | Demo #1: SPI Flash (W25Q32) | Demo #1: I2C Sensor (LM75A) | Demo #2: Qubx Hardware Risk Gate |
|---|---|---|---|---|
| `P0` (`uio[0]`) | **25** | `SCK` (CLK) | `SCL` *(+ 4.7 kΩ pull-up to 3.3V)* | `ORDER_RX` (3 Mbaud UART in from Qubx/Pico 2) |
| `P1` (`uio[1]`) | **26** | `MOSI` (DI) | `SDA` *(+ 4.7 kΩ pull-up to 3.3V)* | `ORDER_TX` (3 Mbaud UART out to Exchange/OME) |
| `P2` (`uio[2]`) | **27** | `MISO` (DO) | — | `ALERT_RL` (High when hardware rate-limit drops an order) |
| `P3` (`uio[3]`) | **28** | `CS_N` | — | `ALERT_KILL` (High when physical kill-switch blocks an order) |
| `P4` (`uio[4]`) | **29** | `nRESET` (active-low) | `nRESET` (active-low) | **RED `KILL` JUMPER WIRE** (`3V3` = Armed; pull to `GND` or hold S2 = KILLED!) |
| `P5` (`uio[5]`) | **30** | Spare | Spare | Spare |
| `P6` (`uio[6]`) | **31** | Spare | Spare | Spare |
| `P7` (`uio[7]`) | **32** | Spare | Spare | Spare |

---

## 5. Onboard Tang Nano 9K LEDs & Pushbuttons (Live Camera Telemetry)

| Hardware | FPGA Pin | Meaning |
|---|---|---|
| **LED 0** | 10 | **Running** — solid ON when SRAM table is loaded and stepping; ~1 Hz heartbeat blink while idle |
| **LED 1** | 11 | **Host Link** — flashes on nibble-link loading & Port-3 trace reads |
| **LED 2** | 13 | **Wire RX / Bus (`P0`/`P1`)** — flashes on incoming orders / SPI / I2C traffic |
| **LED 3** | 14 | **Wire TX / Alert (`P2`/`P3`)** — flashes on outgoing orders / rate-limit alerts |
| **LED 4** | 15 | **KILL / `nRESET` (`P4`)** — solid ON whenever physical kill-switch is engaged |
| **LED 5** | 16 | **Overflow (`uo_out[7]`)** — lights if any FIFO overflow or host drop occurs |
| **Button S1** | 4 | Hardware **Reset** (active-low) |
| **Button S2** | 3 | Backup physical **KILL-SWITCH** button (forces `P4` low while held!) |
