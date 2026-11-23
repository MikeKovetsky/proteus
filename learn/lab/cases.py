# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The demonstration's cases and the negative controls, each with what it must come to."""

from collections import namedtuple

from . import board, devices, jail, judge

Case = namedtuple("Case", "name bus device told verdict code why")

DEMONSTRATION = (
    Case("flash", "spi", devices.flash, {"serve": 5000}, judge.EXACT, "ok",
         "modes, a sequenced reset, a hidden test mode and a hidden key"),
    Case("sensor", "i2c", devices.sensor, {}, judge.EXACT, "ok",
         "a repeated-START pointer, run modes, a hidden register and a hidden second address"),
    Case("thermometer", "i2c", devices.thermometer, {}, judge.EXACT, "ok",
         "nothing hidden: questioning has little to add"),
)

LOCK = {"rate": 5000, "serve": 2000, "inputs": devices.LOCK_INPUTS}

CONTROLS = (
    Case("16-step lock, size told", "spi", lambda: devices.lock(16), dict(LOCK, max_states=18), judge.EXACT, "ok",
         "the failure of the feasibility probe: found once the bound covers it"),
    Case("24-step lock, size told", "spi", lambda: devices.lock(24), dict(LOCK, max_states=26), judge.REFUSED,
         "bound", "the bound cannot be tested within the budget, so nothing is written"),
    Case("24-step lock, size not told", "spi", lambda: devices.lock(24), dict(LOCK), judge.MISSED, "ok",
         "the limit of any black-box method: missed, and the warning says it could be"),
    Case("silent two-byte key", "spi", devices.silent_key, {"serve": 2000}, judge.EXACT, "ok",
         "two bytes in a row that show nothing alone: found by the pairs search"),
    Case("silent three-byte key", "spi", lambda: devices.silent_key(3), {"serve": 2000}, judge.MISSED, "ok",
         "three bytes in a row that show nothing before the last: missed, and the warning says it could be"),
    Case("noisy flash", "spi", devices.flaky, {}, judge.REFUSED, "nondeterministic",
         "0.2% of the bytes it sends have a bit flipped"),
    Case("flash with a time-out", "spi", devices.forgetful, {}, judge.REFUSED, "timing",
         "its write enable runs out after 100 us"),
    Case("flash reset cannot clear", "spi", devices.stubborn, {}, judge.REFUSED, None,
         "nRESET does not bring back one state"),
    Case("flash answering the byte coming in", "spi", devices.clairvoyant, {}, judge.REFUSED, "unsupported",
         "MISO depends on MOSI within the byte"),
    Case("data store", "spi", devices.store, {}, judge.REFUSED, None,
         "it stores any byte: far more states than a table holds"),
    Case("empty SPI bus", "spi", None, {}, judge.REFUSED, "nothing", "no device on the board"),
    Case("empty I2C bus", "i2c", None, {}, judge.REFUSED, "nothing", "no device on the board"),
)


def run(case, seed=1, rates=None, limit_s=jail.LIMIT_S):
    """Learn it in the jail, then judge it. The device is off the board before any clone is loaded."""
    told = dict(case.told, bus=case.bus, seed=seed)
    table = case.device() if case.device else None
    with board.Board(case.bus, seed) as on:
        if table:
            on.plug(table)
        result = jail.learn(on, told, limit_s=limit_s)
        if table:
            report = judge.judge(on, table, result, rates)
        elif result["ok"]:
            report = {"verdict": judge.SILENT}
        else:
            report = {"verdict": judge.ERROR if result["code"] == "error" else judge.REFUSED}
    report["expected"] = report["verdict"] == case.verdict and case.code in (None, result["code"])
    return result, report
