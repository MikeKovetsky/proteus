# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""
CI's layout report (.github/scripts/layout_report.py) against synthetic run folders.

The folders and files are named as LibreLane 3.1.0.dev4 names them (GitHub run 44105853045's file list),
checks.rpt is laid out as its sta/corner.tcl and OpenSTA's ReportPath::reportLimitShort write it, and sta.log
has the metric lines that its common/io.tcl writes. A step's state_out.json is cumulative: it holds the figures
of the earlier STA steps under the same names, which are clean in every test here.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, ".github", "scripts", "layout_report.py")
spec = importlib.util.spec_from_file_location("layout_report", SCRIPT)
layout_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layout_report)

CORNERS = ("nom_fast_1p32V_m40C", "nom_slow_1p08V_125C", "nom_typ_1p20V_25C")
SLOW = "nom_slow_1p08V_125C"
STEP = "58-openroad-stapostpnr"
RULE = "=" * 75
# final/metrics.json's other sign-off figures
SIGN_OFF = {"route__drc_errors": 0, "design__lvs_error__count": 0,
            "antenna__violating__nets": 0, "antenna__violating__pins": 0}


def clean_metrics():
    metrics = {}
    for corner in CORNERS:
        for name, value in (("design__max_slew_violation__count", 0), ("design__max_cap_violation__count", 0),
                            ("design__max_fanout_violation__count", 82), ("timing__setup__ws", 3.712345),
                            ("timing__setup_vio__count", 0), ("timing__hold__ws", 0.091234),
                            ("timing__hold_vio__count", 0)):
            metrics["%s__corner:%s" % (name, corner)] = value
    return metrics


def sta_log(metrics, corner):
    """One corner's sta.log: write_metric_int and write_metric_num print a marker, the name and the value."""
    lines = ["Reading timing models for corner %s..." % corner, "%OL_CREATE_REPORT checks.rpt"]
    for key, value in metrics.items():
        if key.endswith("__corner:" + corner):
            lines.append("%%OL_METRIC_%s %s %s" % ("F" if "__ws__" in key else "I", key, value))
    return "\n".join(lines + ["%OL_END_REPORT"]) + "\n"


def row(pin, limit, value):
    """A row of reportLimitShort: the name padded to 36 places, then three numbers at six digits."""
    return "%-36s %6s %6s %6s (VIOLATED)" % (pin, "%.6f" % limit, "%.6f" % value, "%.6f" % (limit - value))


def checks(slew=(), cap=(), fanout=()):
    lines = ["", RULE, "report_checks --slack_max -0.01", RULE, "",
             "                                       slack (VIOLATED)", "",
             RULE, " report_check_types -max_slew -max_cap -max_fanout -violators", RULE,
             "======================= Corner ===================================", ""]
    for heading, rows in (("max slew", slew), ("max fanout", fanout), ("max capacitance", cap)):
        if rows:
            lines += [heading, "", "%-36s %6s %6s %6s" % ("Pin", "Limit", "Value", "Slack"), "-" * 57]
            lines += list(rows) + [""]
    # The last line here stands for a later report's line that also ends in (VIOLATED).
    lines += ["", RULE, "report_parasitic_annotation -report_unannotated", RULE, "",
              "later_report (VIOLATED)",
              RULE, "max slew violation count %d" % len(slew), "max fanout violation count %d" % len(fanout),
              "max cap violation count %d" % len(cap), RULE]
    return "\n".join(lines) + "\n"


class Report(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.run_dir = folder.name

    def write(self, path, content):
        path = os.path.join(self.run_dir, path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content, indent=4))

    def layout(self, metrics=None, reports=None, final=True, sign_off=None, step=STEP):
        metrics = clean_metrics() if metrics is None else metrics
        self.write(os.path.join(step, "state_out.json"), {"nl": "x.nl.v", "metrics": clean_metrics()})
        for corner in sorted(set(CORNERS) | {key.split("__corner:")[1] for key in metrics}):
            self.write(os.path.join(step, corner, "checks.rpt"), (reports or {}).get(corner, checks()))
            self.write(os.path.join(step, corner, "sta.log"), sta_log(metrics, corner))
        if final:
            self.write(os.path.join("final", "metrics.json"),
                       dict(clean_metrics(), **(SIGN_OFF if sign_off is None else sign_off)))

    def result(self):
        lines, status = layout_report.report(self.run_dir)
        return "\n".join(lines), status

    def test_clean_passes_and_prints_every_figure_in_full(self):
        self.layout()
        text, status = self.result()
        self.assertEqual(status, 0, text)
        self.assertIn("PASS", text)
        self.assertNotIn("FAIL", text)
        for corner in CORNERS:
            line = next(li for li in text.splitlines() if li.startswith(corner))
            self.assertEqual(line.split(), [corner, "0", "0", "82", "3.7123", "0", "0.0912", "0"])
        self.assertIn("route__drc_errors: 0", text)
        self.assertIn("design__lvs_error__count: 0", text)
        self.assertIn("antenna__violating__nets: 0", text)

    def test_each_gated_count_fails_in_each_corner(self):
        for metric, word in (("design__max_slew_violation__count", "max slew"),
                             ("design__max_cap_violation__count", "max cap"),
                             ("timing__setup_vio__count", "setup"), ("timing__hold_vio__count", "hold")):
            for corner in CORNERS:
                metrics = clean_metrics()
                metrics["%s__corner:%s" % (metric, corner)] = 1
                self.layout(metrics)
                text, status = self.result()
                self.assertEqual(status, 1, metric + corner)
                self.assertIn("FAIL: %s: 1 %s violations" % (corner, word), text)

    def test_fanout_is_printed_and_does_not_fail(self):
        metrics = clean_metrics()
        metrics["design__max_fanout_violation__count__corner:" + SLOW] = 97
        self.layout(metrics, {SLOW: checks(fanout=["%-36s %6d %6d %6d (VIOLATED)" % ("clkbuf_4_1_0_clk/X", 8, 16, -8)])})
        text, status = self.result()
        self.assertEqual(status, 0, text)
        self.assertIn("97", text)
        self.assertIn("clkbuf_4_1_0_clk/X 8 16 -8", text)

    def test_wide_count_is_not_cut(self):
        metrics = clean_metrics()
        metrics["design__max_slew_violation__count__corner:" + SLOW] = 123456
        self.layout(metrics)
        self.assertIn("123456", self.result()[0])

    def test_prints_the_violating_pins(self):
        slew = r"core.table_ram.ram_a/A_BM\[31\]"
        cap = r"core.table_ram.ram_a/A_DOUT\[29\]"
        long_name = "core.table_ram." + "x" * 50 + "/A"
        self.layout(reports={SLOW: checks(slew=[row(slew, 0.5952, 1.43), row(long_name, 0.5952, 0.7)],
                                          cap=[row(cap, 0.064, 0.079)])})
        text, _ = self.result()
        self.assertIn("max slew: 2 listed", text)
        self.assertIn(slew + " 0.595200 1.430000 -0.834800", text)
        self.assertIn(long_name + " 0.595200 0.700000", text)
        self.assertIn("max capacitance: 1 listed", text)
        self.assertIn(cap + " 0.064000 0.079000 -0.015000", text)
        # A path's "slack (VIOLATED)" is not a pin.
        self.assertNotIn("    slack", text)
        # Nor is anything below the rule that closes the lists.
        self.assertNotIn("later_report", text)

    def test_pin_list_is_bounded(self):
        limit = 100
        self.assertEqual(layout_report.PINS_PER_SECTION, limit)
        self.layout(reports={SLOW: checks(slew=[row("u%d/A" % i, 0.5, 0.9) for i in range(limit + 25)])})
        text, _ = self.result()
        self.assertIn("max slew: %d listed" % (limit + 25), text)
        self.assertIn("    u%d/A " % (limit - 1), text)
        self.assertNotIn("    u%d/A " % limit, text)
        self.assertIn("and 25 more", text)

    def test_missing_figure_fails_though_an_earlier_step_left_one(self):
        for key in clean_metrics():
            metrics = clean_metrics()
            del metrics[key]
            self.layout(metrics)
            text, status = self.result()
            self.assertEqual(status, 1, key)
            self.assertIn("MISSING", text)

    def test_missing_sta_log_fails(self):
        self.layout()
        os.remove(os.path.join(self.run_dir, STEP, SLOW, "sta.log"))
        text, status = self.result()
        self.assertEqual(status, 1)
        self.assertIn("FAIL: %s: no max slew figure" % SLOW, text)

    def test_a_figure_only_counts_at_the_start_of_a_line_and_under_its_own_corner(self):
        key = "design__max_slew_violation__count__corner:" + SLOW
        metrics = clean_metrics()
        del metrics[key]
        self.layout(metrics)
        fast = "design__max_slew_violation__count__corner:" + CORNERS[0]
        log = sta_log(metrics, SLOW) + "echo %%OL_METRIC_I %s 0\n%%OL_METRIC_I %s 0\n" % (key, fast)
        self.write(os.path.join(STEP, SLOW, "sta.log"), log)
        self.assertEqual(self.result()[1], 1)

    def test_figure_that_is_not_a_number_fails(self):
        for value in (None, "", "1.5", "n/a"):
            metrics = clean_metrics()
            metrics["design__max_cap_violation__count__corner:" + SLOW] = value
            self.layout(metrics)
            self.assertEqual(self.result()[1], 1, repr(value))

    def test_routing_lvs_and_antenna_violations_fail(self):
        for key in SIGN_OFF:
            self.layout(sign_off=dict(SIGN_OFF, **{key: 2}))
            text, status = self.result()
            self.assertEqual(status, 1, key)
            self.assertIn("FAIL: %s is 2" % key, text)

    def test_missing_routing_lvs_or_antenna_figure_fails(self):
        for key in SIGN_OFF:
            self.layout(sign_off={k: v for k, v in SIGN_OFF.items() if k != key})
            text, status = self.result()
            self.assertEqual(status, 1, key)
            self.assertIn("FAIL: no %s figure" % key, text)
            for value in (None, "0", False):
                self.layout(sign_off=dict(SIGN_OFF, **{key: value}))
                self.assertEqual(self.result()[1], 1, "%s %r" % (key, value))

    def test_no_final_folder_fails_though_timing_is_clean(self):
        self.layout(final=False)
        text, status = self.result()
        self.assertEqual(status, 1)
        self.assertIn("FAIL: no route__drc_errors figure", text)
        self.assertNotIn("PASS", text)

    def test_infinite_slack_prints(self):
        metrics = clean_metrics()
        metrics["timing__hold__ws__corner:" + SLOW] = float("inf")
        self.layout(metrics)
        text, status = self.result()
        self.assertEqual(status, 0, text)
        self.assertIn("inf", text)

    def test_a_corner_it_does_not_know_is_gated_too(self):
        metrics = clean_metrics()
        for key, value in clean_metrics().items():
            if key.endswith(SLOW):
                metrics[key.replace(SLOW, "max_slow_corner")] = value
        metrics["design__max_slew_violation__count__corner:max_slow_corner"] = 3
        self.layout(metrics)
        text, _ = self.result()
        self.assertIn("FAIL: max_slow_corner: 3 max slew violations", text)
        self.assertIn("-- max_slow_corner", text)

    def test_reports_without_a_final_folder(self):
        # A later step stopped the flow, so there is no final/metrics.json.
        metrics = clean_metrics()
        metrics["design__max_slew_violation__count__corner:" + SLOW] = 78
        self.layout(metrics, final=False)
        text, status = self.result()
        self.assertEqual(status, 1)
        self.assertIn("78", text)
        self.assertIn("final/metrics.json: none", text)

    def test_mid_pnr_figures_never_stand_in(self):
        for step in ("12-openroad-staprepnr", "45-openroad-stamidpnr-3"):
            self.write(step + "/state_out.json", {"metrics": clean_metrics()})
            for corner in CORNERS:
                self.write(os.path.join(step, corner, "sta.log"), sta_log(clean_metrics(), corner))
        self.write("final/metrics.json", dict(clean_metrics(), **SIGN_OFF))
        text, status = self.result()
        self.assertEqual(status, 1)
        self.assertIn("no post-PNR timing figures", text)

    def test_empty_absent_or_broken_run_fails(self):
        self.assertEqual(self.result()[1], 1)
        self.assertEqual(layout_report.report(os.path.join(self.run_dir, "absent"))[1], 1)
        self.layout()
        self.write(os.path.join(STEP, "state_out.json"), "{ cut sho")
        self.assertEqual(self.result()[1], 1)
        os.remove(os.path.join(self.run_dir, STEP, "state_out.json"))
        self.assertEqual(self.result()[1], 1)

    def test_two_post_pnr_steps_fail(self):
        self.layout()
        self.layout(step="99-openroad-stapostpnr")
        self.assertEqual(self.result()[1], 1)

    def test_missing_checks_report_fails(self):
        self.layout()
        os.remove(os.path.join(self.run_dir, STEP, SLOW, "checks.rpt"))
        self.assertIn("FAIL: %s: no checks.rpt" % SLOW, self.result()[0])

    def test_counts_spef_warnings(self):
        self.layout()
        log = ("Warning: /w/runs/wokwi/57-openroad-rcx/nom/top.nom.spef line 96362, net a not found.\n"
               "[WARNING STA-1648] /w/top.nom.spef line 7, instance b not found.\n"
               "Warning: something else\nReading top.nom.spef\n")
        self.write(os.path.join(STEP, SLOW, "sta.log"), sta_log(clean_metrics(), SLOW) + log)
        self.assertIn("parasitics warnings while reading SPEF: 2", self.result()[0])

    def test_exit_status_as_ci_runs_it(self):
        self.layout()
        command = [sys.executable, SCRIPT, self.run_dir]
        self.assertEqual(subprocess.run(command, stdout=subprocess.PIPE).returncode, 0)
        metrics = clean_metrics()
        metrics["design__max_cap_violation__count__corner:" + SLOW] = 1
        self.layout(metrics)
        failed = subprocess.run(command, stdout=subprocess.PIPE)
        self.assertEqual(failed.returncode, 1)
        self.assertIn(b"1 max cap violations", failed.stdout)


if __name__ == "__main__":
    unittest.main()
