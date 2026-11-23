# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Independent third-party protocol oracle verification against tt_um_mikekovetsky_protoemu RTL.

Every protocol waveform below is judged or driven by an external open-source cocotb VIP written
independently of this repository:
  - cocotbext-uart (Alex Forencich): UartSource / UartSink
  - cocotbext-i2c  (Alex Forencich): I2cMemory
  - cocotbext-spi  (Spencer Chang):  SpiMaster / SpiSlaveLoopback
"""

import cocotb
from cocotb.handle import Immediate, LogicObject
from cocotb.triggers import ClockCycles, Timer

# cocotb 2.x removed LogicObject.setimmediatevalue (used by cocotbext-uart 0.1.4 and cocotbext-i2c 0.1.2).
if not hasattr(LogicObject, "setimmediatevalue"):
    LogicObject.setimmediatevalue = lambda self, v: self.set(Immediate(v))

from cocotbext.i2c import I2cMemory
from cocotbext.spi import SpiBus, SpiConfig, SpiMaster
from cocotbext.spi.devices.generic import SpiSlaveLoopback
from cocotbext.uart import UartSink, UartSource

from host_link import PORT_CH0_CH1, PORT_CH3_CH2, Host, program, reset, start_clock
from bakeoff.cand_table.compiler import compile_text
from test_v2 import SPI_5MHZ_TCAM_NRESET


async def _boot(dut, bus_mode, img, start_clk=True):
    dut.ena.value = 1
    dut.bus_mode.value = bus_mode
    dut.uart_rx.value = 1
    dut.i2c_scl_drv.value = 1
    dut.i2c_sda_drv.value = 1
    dut.spi_c_miso.value = 1
    dut.spi_t_sclk.value = 0
    dut.spi_t_mosi.value = 0
    dut.spi_t_cs.value = 1
    dut.spi_t_nrst.value = 1
    if start_clk:
        start_clock(dut)
    host = Host(dut)
    await reset(dut, host)
    await host.load(img)
    await ClockCycles(dut.clk, 10)
    return host


@cocotb.test()
async def test_oracle_uart_115200(dut):
    """UART 115,200 baud TX and RX verified by Alex Forencich's cocotbext-uart."""
    # TX
    host = await _boot(dut, 0, program("uart_tx"))
    sink = UartSink(dut.uart_tx, baud=115200, bits=8, stop_bits=1)
    tx_payload = bytes([0x55, 0xA3, 0x00, 0xFF, 0x42])
    await host.send(PORT_CH0_CH1, tx_payload)
    got_tx = bytearray()
    for _ in tx_payload:
        got_tx.append((await sink.read(1))[0])
    assert bytes(got_tx) == tx_payload, f"UartSink decoded {got_tx.hex()}, want {tx_payload.hex()}"

    # RX
    host = await _boot(dut, 0, program("uart_rx"), start_clk=False)
    src = UartSource(dut.uart_rx, baud=115200, bits=8, stop_bits=1)
    rx_payload = bytes([0xDE, 0xAD, 0xBE, 0xEF, 0x01])
    await src.write(rx_payload)
    got_rx = []
    for _ in rx_payload:
        got_rx.append(await host.read(PORT_CH0_CH1, limit=20000))
    await src.wait()
    assert bytes(got_rx) == rx_payload, f"Chip UART RX got {bytes(got_rx).hex()}, want {rx_payload.hex()}"


@cocotb.test()
async def test_oracle_uart_921600(dut):
    """UART 921,600 baud TX and RX verified by cocotbext-uart."""
    host = await _boot(dut, 0, program("uart_tx_921600"))
    sink = UartSink(dut.uart_tx, baud=921600, bits=8, stop_bits=1)
    tx_payload = bytes([0xA5, 0x5A, 0xC3, 0x3C])
    await host.send(PORT_CH0_CH1, tx_payload)
    got_tx = bytearray()
    for _ in tx_payload:
        got_tx.append((await sink.read(1))[0])
    assert bytes(got_tx) == tx_payload, f"921600 TX decoded {got_tx.hex()}, want {tx_payload.hex()}"

    host = await _boot(dut, 0, program("uart_rx_921600"), start_clk=False)
    src = UartSource(dut.uart_rx, baud=921600, bits=8, stop_bits=1)
    rx_payload = bytes([0x12, 0x34, 0x56, 0x78])
    await src.write(rx_payload)
    got_rx = [await host.read(PORT_CH0_CH1, limit=20000) for _ in rx_payload]
    await src.wait()
    assert bytes(got_rx) == rx_payload, f"921600 RX got {bytes(got_rx).hex()}, want {rx_payload.hex()}"


@cocotb.test()
async def test_oracle_i2c_eeprom_400k(dut):
    """I2C 400 kHz controller verified against Alex Forencich's cocotbext-i2c I2cMemory."""
    host = await _boot(dut, 1, program("i2c_ctrl"))
    mem = I2cMemory(sda=dut.i2c_sda, sda_o=dut.i2c_sda_drv, scl=dut.i2c_scl, scl_o=dut.i2c_scl_drv,
                    addr=0x50, size=256)

    # 1. Write 4 bytes at EEPROM address 0x10: [addr, nw, reg, d0..d3, nr=0]
    await host.send(PORT_CH0_CH1, (0x50, 5, 0x10, 0xDE, 0xAD, 0xBE, 0xEF, 0))
    st = await host.read(PORT_CH3_CH2, limit=40000)
    assert st == 0x00, f"I2C write status 0x{st:02x}"
    assert mem.read_mem(0x10, 4) == b"\xDE\xAD\xBE\xEF", f"EEPROM holds {mem.read_mem(0x10, 4).hex()}"

    # 2. Repeated-START write-then-read from address 0x10
    await host.send(PORT_CH0_CH1, (0x50, 1, 0x10, 4))
    got = [await host.read(PORT_CH0_CH1, limit=40000) for _ in range(4)]
    st = await host.read(PORT_CH3_CH2, limit=40000)
    assert st == 0x00 and got == [0xDE, 0xAD, 0xBE, 0xEF], f"I2C W+R got {got}, st={st}"

    # 3. v2 plain read (bit 7 set on addr, nw=0, nr=2) after setting EEPROM pointer to 0x11
    await host.send(PORT_CH0_CH1, (0x50, 1, 0x11, 0))
    assert await host.read(PORT_CH3_CH2, limit=40000) == 0x00
    await host.send(PORT_CH0_CH1, (0x50 | 0x80, 0, 2))
    plain = [await host.read(PORT_CH0_CH1, limit=40000) for _ in range(2)]
    st = await host.read(PORT_CH3_CH2, limit=40000)
    assert st == 0x00 and plain == [0xAD, 0xBE], f"I2C plain read got {plain}, st={st}"

    # 4. Missing address -> NACK status 0x01
    await host.send(PORT_CH0_CH1, (0x51, 1, 0x00, 0))
    st = await host.read(PORT_CH3_CH2, limit=40000)
    assert st == 0x01, f"Expected NACK (0x01) for 0x51, got 0x{st:02x}"


@cocotb.test()
async def test_oracle_i2c_eeprom_100k(dut):
    """I2C 100 kHz controller verified against cocotbext-i2c I2cMemory."""
    host = await _boot(dut, 1, program("i2c_ctrl_100k"))
    mem = I2cMemory(sda=dut.i2c_sda, sda_o=dut.i2c_sda_drv, scl=dut.i2c_scl, scl_o=dut.i2c_scl_drv,
                    addr=0x50, size=256)

    await host.send(PORT_CH0_CH1, (0x50, 3, 0x20, 0xCA, 0xFE, 0))
    assert await host.read(PORT_CH3_CH2, limit=80000) == 0x00
    assert mem.read_mem(0x20, 2) == b"\xCA\xFE"

    await host.send(PORT_CH0_CH1, (0x50, 1, 0x20, 2))
    got = [await host.read(PORT_CH0_CH1, limit=80000) for _ in range(2)]
    st = await host.read(PORT_CH3_CH2, limit=80000)
    assert st == 0x00 and got == [0xCA, 0xFE], f"I2C 100k got {got}, st={st}"


@cocotb.test()
async def test_oracle_spi_controllers_all_modes(dut):
    """SPI controllers (modes 0..3) verified against Spencer Chang's cocotbext-spi SpiSlaveLoopback."""
    start_clock(dut)
    bus = SpiBus.from_prefix(dut, "spi_c", cs_name="cs")
    for mode, (cpol, cpha) in enumerate(((False, False), (False, True), (True, False), (True, True))):
        host = await _boot(dut, 2, program(f"spi_ctrl_m{mode}"), start_clk=False)
        slave = SpiSlaveLoopback(bus, SpiConfig(word_width=8, cpol=cpol, cpha=cpha, frame_spacing_ns=1))
        await Timer(10, unit="ns")

        # Send 3 single-byte frames; SpiSlaveLoopback returns the previous frame's byte each time.
        sent = [0xA5, 0x3C, 0x5A]
        expected_rx = [0x00, 0xA5, 0x3C]
        for tx, exp in zip(sent, expected_rx):
            await host.send(PORT_CH0_CH1, (1, tx))
            got = await host.read(PORT_CH0_CH1, limit=5000)
            assert got == exp, f"SPI mode {mode}: tx=0x{tx:02x} got 0x{got:02x}, want 0x{exp:02x}"
            await Timer(200, unit="ns")
        assert await slave.get_contents() == 0x5A, f"SPI mode {mode}: slave final byte mismatch"
        slave._run_coroutine_obj.cancel()


@cocotb.test()
async def test_oracle_spi_target_v2_tcam_5mhz(dut):
    """RTL v2 5 MHz TCAM SPI target with nRESET verified against cocotbext-spi SpiMaster."""
    await _boot(dut, 3, compile_text(SPI_5MHZ_TCAM_NRESET).image)
    bus = SpiBus.from_prefix(dut, "spi_t", cs_name="cs")
    master = SpiMaster(bus, SpiConfig(word_width=8, sclk_freq=5e6, cpol=False, cpha=False, frame_spacing_ns=200))

    # 0x03 -> TCAM class 1 -> next byte answers 0x5A; then 0x00 -> default -> R=0x00
    await master.write([0x03, 0x00], burst=True)
    r1 = list(await master.read())
    assert r1 == [0x00, 0x5A], f"5 MHz TCAM frame 1 got {[hex(x) for x in r1]}, want [0x00, 0x5a]"

    # 0x87 -> TCAM class 2 (masked range 0x80..0x8F) -> next byte answers 0xC3; send 0x87 again to keep R=0xC3
    await master.write([0x87, 0x87], burst=True)
    r2 = list(await master.read())
    assert r2 == [0x00, 0xC3], f"5 MHz TCAM frame 2 got {[hex(x) for x in r2]}, want [0x00, 0xc3]"

    # Confirm R=0xC3 persisted across CS high, then send 0x87 to keep R=0xC3 before nRESET
    await master.write([0x87], burst=False)
    assert list(await master.read()) == [0xC3]

    # Hardware nRESET on P4 -> state returns to 0x00 (not 0xC3)
    dut.spi_t_nrst.value = 0
    await ClockCycles(dut.clk, 10)
    dut.spi_t_nrst.value = 1
    await ClockCycles(dut.clk, 10)

    await master.write([0x00], burst=False)
    r3 = list(await master.read())
    assert r3 == [0x00], f"5 MHz TCAM after nRESET got {[hex(x) for x in r3]}, want [0x00]"
    master._run_coroutine_obj.cancel()
    master._SpiClock._run_coroutine_obj.cancel()
    await ClockCycles(dut.clk, 2)
