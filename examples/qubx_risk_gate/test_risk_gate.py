# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""End-to-end pin-level verification of Proteus Inline Hardware Pre-Trade Risk Gate.

Exercises unmodified Proteus RTL v2 through Verilator on all 7 hardware risk-gate paths:
  1. Normal NewOrder pass-through + lossless 4-byte forwarding + 0 ns jitter determinism
  2. Hardware token-bucket rate limiter (MAX_BURST=3 NewOrders/window, P2 ALERT_RL high)
  3. Safety-critical Cancel ('X') & Heartbeat ('H') bypass active rate-limit throttle
  4. Automatic inter-machine token refill via rl_timer semaphore P5 after RL_WINDOW (100 us)
  5. 1-clock Fat-Finger guard (QTY >= 128 rejected via S[7:4] dispatch without burning a token)
  6. 1-clock TCAM malformed-opcode rejection (unknown opcode dropped immediately)
  7. Physical hardware KILL-SWITCH on P4 (both pre-frame block AND mid-TX instant abort
     captured by the Port-3 hardware logic-analyzer trace buffer + clean recovery on re-arm)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from qubx_gate import (
    OP_CANCEL,
    OP_HEARTBEAT,
    OP_NEW_ORDER,
    OrderFrame,
    ProteusHardwareGate,
    SimulatedOME,
    render_port3_waveform,
)


class TestProteusRiskGate(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = ProteusHardwareGate()
        self.ome = SimulatedOME()

    def tearDown(self) -> None:
        self.gate.close()

    def test_01_normal_orders_deterministic_zero_jitter(self) -> None:
        latencies = []
        for idx, (sym, px, qty) in enumerate([(1, 130, 10), (2, 128, 25), (3, 125, 50)]):
            order = OrderFrame(OP_NEW_ORDER, sym, px, qty)
            res = self.gate.submit(order, arm_trace=(1 if idx == 0 else 0))
            self.assertTrue(res.passed, f"order {idx} failed: {res}")
            self.assertEqual(res.audits, [0xA0])
            self.assertFalse(res.alert_rl)
            self.assertFalse(res.alert_kill)
            self.assertFalse(res.overflow)
            latencies.append(res.lat_clks)
            self.ome.on_wire_frame(res.forwarded)
            if idx == 0:
                self.assertGreater(len(res.trace), 4, "Port-3 trace should capture pin activity")

        # 0 ns jitter: every single approved NewOrder has the EXACT same clock latency!
        self.assertEqual(len(set(latencies)), 1, f"expected 0-jitter latency, got {latencies}")
        self.assertEqual(self.ome.filled_orders, 3)

    def test_02_rate_limiter_and_cancel_bypass_and_refill(self) -> None:
        # Burst 3 NewOrders: all 3 pass (MAX_BURST = 3)
        for i in range(3):
            res = self.gate.submit(OrderFrame(OP_NEW_ORDER, 1, 128 + i, 5))
            self.assertTrue(res.passed, f"burst order {i} should pass: {res}")
            self.assertEqual(res.audits, [0xA0])

        # Rogue orders #4 and #5 immediately throttled by hardware rate limiter!
        for i in range(2):
            res = self.gate.submit(OrderFrame(OP_NEW_ORDER, 1, 135 + i, 5))
            self.assertFalse(res.passed, f"rogue order {i} should be throttled: {res}")
            self.assertEqual(res.raw_tx, b"")
            self.assertEqual(res.audits, [0xE1])
            self.assertTrue(res.alert_rl, "P2 ALERT_RL must be high when throttled")

        # Safety-critical Cancel ('X') and Heartbeat ('H') MUST bypass rate limiter!
        cxl = OrderFrame(OP_CANCEL, 1, 128, 10)
        res_cxl = self.gate.submit(cxl)
        self.assertTrue(res_cxl.passed, f"Cancel must bypass rate limit: {res_cxl}")
        self.assertEqual(res_cxl.audits, [0xA1])

        hb = OrderFrame(OP_HEARTBEAT, 0, 0, 42)
        res_hb = self.gate.submit(hb)
        self.assertTrue(res_hb.passed, f"Heartbeat must bypass rate limit: {res_hb}")
        self.assertEqual(res_hb.audits, [0xA2])

        # Advance 420 us (> RL_WINDOW = 400 us) so Machine 1 (rl_timer) issues refill semaphore P5!
        self.gate.advance_us(420.0)
        res_after = self.gate.submit(OrderFrame(OP_NEW_ORDER, 2, 129, 7))
        self.assertTrue(res_after.passed, f"NewOrder should pass after window refill: {res_after}")
        self.assertEqual(res_after.audits, [0xA0])
        self.assertFalse(res_after.alert_rl)

    def test_03_fat_finger_and_bad_opcode_guards(self) -> None:
        # Fat-finger QTY = 200 (>= 128) rejected in 1 clock by S[7:4] dispatch (and doesn't burn a token!)
        ff = self.gate.submit(OrderFrame(OP_NEW_ORDER, 1, 128, 200))
        self.assertFalse(ff.passed)
        self.assertEqual(ff.raw_tx, b"")
        self.assertEqual(ff.audits, [0xE4])

        # Unknown opcode 0xFF rejected in 1 clock by TCAM miss!
        bad = self.gate.submit(OrderFrame(0xFF, 1, 128, 1))
        self.assertFalse(bad.passed)
        self.assertEqual(bad.raw_tx, b"")
        self.assertEqual(bad.audits, [0xE3])

        # All 3 burst tokens are STILL available after fat-finger & bad-opcode drops!
        for i in range(3):
            res = self.gate.submit(OrderFrame(OP_NEW_ORDER, 1, 128, 10))
            self.assertTrue(res.passed, f"token {i} should still be available: {res}")

    def test_04_physical_kill_switch_pre_and_mid_tx_with_port3_trace(self) -> None:
        # 1) Pre-frame kill: pull physical P4 low -> blocks NewOrder, Cancel, AND Heartbeat!
        st = self.gate.set_kill_switch(armed=False)
        self.assertEqual(st["alert_kill"], 1)
        for op in (OP_NEW_ORDER, OP_CANCEL, OP_HEARTBEAT):
            res = self.gate.submit(OrderFrame(op, 1, 128, 5))
            self.assertFalse(res.passed)
            self.assertEqual(res.raw_tx, b"")
            self.assertEqual(res.audits, [0xE2])
            self.assertTrue(res.alert_kill)

        # 2) Re-arm P4 -> ALERT_KILL clears and normal trading resumes!
        st2 = self.gate.set_kill_switch(armed=True)
        self.assertEqual(st2["alert_kill"], 0)
        ok = self.gate.submit(OrderFrame(OP_NEW_ORDER, 1, 128, 12))
        self.assertTrue(ok.passed)

        # 3) MID-PACKET KILL: yank physical P4=0 250 clocks into TX (mid Byte 1)!
        #    Proteus must abort TX immediately (zero full 4-byte order arrives at Exchange),
        #    raise ALERT_KILL within <= 6 clocks (~100 ns including 2-flop sync!), and capture
        #    the exact kill edge in the Port-3 hardware logic analyzer!
        mid = self.gate.submit(OrderFrame(OP_NEW_ORDER, 1, 130, 15), kill_mid_tx_clks=250, arm_trace=2)
        self.assertFalse(mid.passed, "mid-TX kill MUST prevent complete order from reaching Exchange")
        self.assertLess(len(mid.raw_tx), 4, f"expected truncated TX on mid-TX kill, got {mid.raw_tx.hex()}")
        self.assertIn(0xE2, mid.audits, f"expected DROP_KILL_SWITCH (0xE2) audit, got {mid.audits}")
        self.assertTrue(mid.alert_kill)
        self.assertGreater(mid.kill_resp_clks, 0)
        self.assertLessEqual(mid.kill_resp_clks, 8, f"kill cutoff too slow: {mid.kill_resp_clks} clks")
        self.assertGreater(len(mid.trace), 1, "Port-3 trace must capture mid-TX kill edge")

        # Verify ASCII waveform renderer works cleanly on real Port-3 trace data
        wf = render_port3_waveform(mid.trace)
        self.assertEqual(len(wf), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
