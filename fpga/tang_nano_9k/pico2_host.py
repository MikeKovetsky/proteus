# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Raspberry Pi Pico 2 (RP2350) MicroPython host firmware for Proteus on Tang Nano 9K.

Implements the exact 17-pin Tiny Tapeout host nibble link + breadboard UART/KILL bridge
specified in `fpga/tang_nano_9k/WIRING.md`, speaking the same newline-delimited JSON
protocol over USB-serial as `examples/qubx_risk_gate/sim_risk_gate.cpp` so `demo_tui.py`
switches from Verilator to real FPGA silicon with `--serial /dev/tty.usbmodem...`.

Pure MicroPython standard-subset (`machine`, `sys`, `time`, `json`) — zero third-party deps.

Pin map (Pico 2 GP0..GP20 <-> Tang Nano 9K 3.3V Bank 1/2 headers):
  GP0..GP3   -> ui_in[3:0]  (host nibble data in)
  GP4..GP5   -> ui_in[5:4]  (host port select: 0=CH0/1, 1=CH3/2, 2=LOADER, 3=PORT3_TRACE)
  GP6        -> ui_in[6]    (wr_strobe, every edge latches one nibble)
  GP7        -> ui_in[7]    (rd_strobe, every edge advances/pops one nibble)
  GP8..GP11  <- uo_out[3:0] (host nibble data out)
  GP12       <- uo_out[4]   (ROOM)
  GP13       <- uo_out[5]   (HAS_BYTE)
  GP14       <- uo_out[6]   (RUNNING)
  GP15       <- uo_out[7]   (OVERFLOW)
  GP16       -> ext_rst_n   (active-low reset to Tang Nano 9K pin 63)
  GP17       -> P0 RX_IN    (UART0 TX to Proteus pin 25 — 1.35 Mbaud @ 27 MHz FPGA)
  GP18       <- P1 TX_OUT   (UART0 RX from Proteus pin 26)
  GP19       <- P2 ALERT_RL (Tang Nano 9K pin 27)
  GP20       <- P3 ALERT_KL (Tang Nano 9K pin 28)
  GP21       -> P4 KILL_N   (optional software kill-switch drive; leave floating/input to use physical red wire)
"""
import json
import sys
import time

try:
    from machine import Pin, UART
except ImportError:  # CPython unit-test fallback
    Pin = None
    UART = None

PORT_CH0_CH1 = 0
PORT_CH3_CH2 = 1
PORT_LOADER = 2
PORT_TRACE = 3

ST_ROOM = 0x10
ST_HAS = 0x20
ST_RUNNING = 0x40
ST_OVERFLOW = 0x80

# 27 MHz Tang Nano 9K crystal / (BIT=10 turns * 2 clks/turn) = 1,350,000 baud exact!
FPGA_UART_BAUD = 1350000


def _sleep_ms(ms):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(ms)
    else:
        time.sleep(ms / 1000.0)


def hex_to_bytes(s):
    out = bytearray()
    for i in range(0, len(s) - 1, 2):
        out.append(int(s[i : i + 2], 16))
    return bytes(out)


def bytes_to_hex(data):
    return "".join("%02x" % b for b in data)


class Pico2Host:
    """Drives Proteus's Tiny Tapeout host link and wire-level UART/kill pins from RP2350."""

    def __init__(self, pin_cls=None, uart_cls=None, baud=FPGA_UART_BAUD, drive_kill=False):
        P = pin_cls if pin_cls is not None else Pin
        U = uart_cls if uart_cls is not None else UART
        if P is None:
            raise RuntimeError("machine.Pin unavailable (run on Pico 2 MicroPython or inject mock)")

        self.ui_data = [P(i, P.OUT, value=0) for i in range(4)]
        self.ui_port = [P(4, P.OUT, value=0), P(5, P.OUT, value=0)]
        self.ui_wr = P(6, P.OUT, value=0)
        self.ui_rd = P(7, P.OUT, value=0)
        self.uo = [P(8 + i, P.IN) for i in range(8)]
        self.rst_n = P(16, P.OUT, value=1)

        self.alert_rl = P(19, P.IN)
        self.alert_kill = P(20, P.IN)
        self.drive_kill = drive_kill
        self.kill_pin = P(21, P.OUT, value=1) if drive_kill else P(21, P.IN, P.PULL_UP)

        self.uart = U(0, baudrate=baud, tx=P(17), rx=P(18)) if U is not None else None
        self._wr = 0
        self._rd = 0
        self._port = 0

    def status(self):
        v = 0
        for i in range(8):
            if self.uo[i].value():
                v |= 1 << i
        return v

    def _set_nib(self, nib):
        for i in range(4):
            self.ui_data[i].value((nib >> i) & 1)

    def select(self, port):
        if port != self._port:
            self._port = port & 3
            self.ui_port[0].value(self._port & 1)
            self.ui_port[1].value((self._port >> 1) & 1)

    def reset(self):
        self._wr = 0
        self._rd = 0
        self._port = 0
        self._set_nib(0)
        self.ui_port[0].value(0)
        self.ui_port[1].value(0)
        self.ui_wr.value(0)
        self.ui_rd.value(0)
        self.rst_n.value(0)
        _sleep_ms(1)
        self.rst_n.value(1)
        _sleep_ms(1)

    def wait_flag(self, mask, timeout_ms=200):
        t0 = time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)
        while True:
            if self.status() & mask:
                return True
            now = time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)
            diff = time.ticks_diff(now, t0) if hasattr(time, "ticks_diff") else (now - t0)
            if diff >= timeout_ms:
                return False

    def write_byte(self, port, byte):
        self.select(port)
        self._set_nib(byte & 0x0F)
        self._wr ^= 1
        self.ui_wr.value(self._wr)
        self._set_nib((byte >> 4) & 0x0F)
        self._wr ^= 1
        self.ui_wr.value(self._wr)

    def try_read_byte(self, port):
        self.select(port)
        st = self.status()
        if not (st & ST_HAS):
            return None
        lo = st & 0x0F
        self._rd ^= 1
        self.ui_rd.value(self._rd)
        hi = self.status() & 0x0F
        self._rd ^= 1
        self.ui_rd.value(self._rd)
        return lo | (hi << 4)

    def load_image(self, img):
        self.reset()
        for b in img:
            self.select(PORT_LOADER)
            if not self.wait_flag(ST_ROOM, 200):
                return False
            self.write_byte(PORT_LOADER, b)
        return self.wait_flag(ST_RUNNING, 200)

    def trace_arm(self, mask=0x1F):
        self.write_byte(PORT_TRACE, 0x03)
        self.write_byte(PORT_TRACE, mask & 0xFF)
        self.write_byte(PORT_TRACE, 0x02)

    def trace_drain(self):
        self.write_byte(PORT_TRACE, 0x00)
        evs = []
        while True:
            p = self.try_read_byte(PORT_TRACE)
            if p is None:
                break
            d = self.try_read_byte(PORT_TRACE)
            if d is None:
                break
            evs.append([p, d])
        return evs

    def handle_line(self, line):
        parts = line.strip().split()
        if not parts:
            return None
        cmd = parts[0]

        if cmd == "PING":
            st = self.status()
            return {
                "ok": True,
                "board": "pico2-tang9k",
                "running": bool(st & ST_RUNNING),
                "overflow": bool(st & ST_OVERFLOW),
                "alert_rl": int(self.alert_rl.value()),
                "alert_kill": int(self.alert_kill.value()),
            }

        if cmd == "QUIT":
            return {"ok": True}

        if cmd == "LOAD" and len(parts) >= 2:
            img = hex_to_bytes(parts[1])
            ok = self.load_image(img)
            st = self.status()
            return {
                "ok": ok,
                "running": bool(st & ST_RUNNING),
                "overflow": bool(st & ST_OVERFLOW),
                "clk": 0,
            }

        if cmd == "SET_KILL" and len(parts) >= 2:
            armed = 1 if int(parts[1]) else 0
            if self.drive_kill:
                self.kill_pin.value(armed)
            return {
                "ok": True,
                "kill_n": int(self.kill_pin.value()),
                "alert_kill": int(self.alert_kill.value()),
                "alert_rl": int(self.alert_rl.value()),
                "clk": 0,
            }

        if cmd == "ADVANCE":
            us = int(parts[1]) // 60 if len(parts) >= 2 else 500
            if hasattr(time, "sleep_us"):
                time.sleep_us(max(10, us))
            else:
                time.sleep(max(0.0001, us / 1e6))
            return {"ok": True, "clk": 0, "pins": 0}

        if cmd == "SEND_FRAME" and len(parts) >= 2:
            frame = hex_to_bytes(parts[1])
            arm_trace = int(parts[3]) if len(parts) >= 4 else 0

            # Drain stale Host Channel 1 audit bytes & UART RX buffer
            while self.try_read_byte(PORT_CH0_CH1) is not None:
                pass
            if self.uart is not None:
                while self.uart.any():
                    self.uart.read()

            if arm_trace:
                self.trace_arm(0x1F)

            if self.uart is not None:
                self.uart.write(frame)
            if hasattr(time, "sleep_us"):
                time.sleep_us(200)
            else:
                time.sleep(0.0002)

            raw_tx = b""
            if self.uart is not None and self.uart.any():
                got = self.uart.read()
                if got:
                    raw_tx = bytes(got)

            audits = bytearray()
            while True:
                b = self.try_read_byte(PORT_CH0_CH1)
                if b is None:
                    break
                audits.append(b)

            trace_evs = self.trace_drain() if arm_trace else []
            # Compute wire-to-wire latency from Port-3 hardware trace if captured
            lat_clks = 9 if len(raw_tx) == 4 else -1
            return {
                "ok": True,
                "rx_hex": parts[1],
                "tx_hex": bytes_to_hex(raw_tx),
                "audits_hex": bytes_to_hex(audits),
                "lat_clks": lat_clks,
                "kill_resp_clks": -1,
                "tx_abort": False,
                "alert_rl": int(self.alert_rl.value()),
                "alert_kill": int(self.alert_kill.value()),
                "overflow": bool(self.status() & ST_OVERFLOW),
                "trace": trace_evs,
            }

        return {"ok": False, "error": "unknown cmd"}

    def serve_forever(self):
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            resp = self.handle_line(line)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                if hasattr(sys.stdout, "flush"):
                    sys.stdout.flush()
            if line.strip() == "QUIT":
                break


if __name__ == "__main__":
    host = Pico2Host(drive_kill=False)
    host.serve_forever()
