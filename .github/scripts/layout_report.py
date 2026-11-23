#!/usr/bin/env python3
# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""
Prints a layout run's sign-off timing and electrical figures in full, and fails on a violation.

    python3 .github/scripts/layout_report.py [runs/wokwi]

LibreLane's own table is cut at 80 columns in CI's log (a count reads "1…"), and the violating pins are only
in report files inside an artifact. This reads both from the post-PNR STA step, OpenROAD.STAPostPNR: each
corner's figures are the metric lines of its own sta.log. state_out.json and final/metrics.json cannot show
them: their metrics are cumulative and the earlier STA steps write the same names, so an estimate would stand
in for a figure that sign-off did not report.

Failing: max slew, max cap, setup and hold violations in any corner, routing, LVS and antenna violations, or a
missing figure. Max fanout is printed and does not fail.
"""
import json
import os
import re
import sys
from glob import glob

CORNERS = ("nom_fast_1p32V_m40C", "nom_typ_1p20V_25C", "nom_slow_1p08V_125C")
# (column, metric, whether a value above 0 fails)
COLUMNS = (
    ("max slew", "design__max_slew_violation__count", True),
    ("max cap", "design__max_cap_violation__count", True),
    ("max fanout", "design__max_fanout_violation__count", False),
    ("setup WS ns", "timing__setup__ws", False),
    ("setup vios", "timing__setup_vio__count", True),
    ("hold WS ns", "timing__hold__ws", False),
    ("hold vios", "timing__hold_vio__count", True),
)
# From final/metrics.json, where the last writer of each is its sign-off step. Above 0 fails, as a missing one
# does: LibreLane's checkers only warn about a missing figure, and on this PDK neither they nor the precheck
# fail on antenna violations.
SIGN_OFF = (
    "route__drc_errors",
    "design__lvs_error__count",
    "antenna__violating__nets",
    "antenna__violating__pins",
)
# The headings OpenSTA's report_check_types prints above each list of violators in checks.rpt.
SECTIONS = ("max slew", "max capacitance", "max fanout")
PINS_PER_SECTION = 100


def post_pnr_step(run_dir):
    steps = [d for d in glob(os.path.join(run_dir, "*-openroad-stapostpnr")) if os.path.isdir(d)]
    return steps[0] if len(steps) == 1 else None


def read_json(path):
    try:
        with open(path, encoding="utf8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def logged_metrics(log):
    """The metrics in one OpenSTA log, from the lines LibreLane itself takes them from."""
    found = {}
    for kind, name, value in re.findall(r"^%OL_METRIC_([IF]) (\S+) (\S+)$", log, re.M):
        try:
            found[name] = int(value) if kind == "I" else float(value)
        except ValueError:
            found[name] = None
    return found


def figures(logs):
    """The table's lines, and what is wrong with the figures, from each corner's own sta.log."""
    lines = ["%-22s" % "corner" + "".join("%13s" % column for column, _, _ in COLUMNS)]
    problems = []
    for corner, log in logs.items():
        metrics = logged_metrics(log)
        cells = []
        for column, metric, fails in COLUMNS:
            value = metrics.get("%s__corner:%s" % (metric, corner))
            if value is None:
                cells.append("MISSING")
                problems.append("%s: no %s figure" % (corner, column))
                continue
            cells.append("%.4f" % value if metric.endswith("__ws") else "%d" % value)
            if fails and value != 0:
                problems.append("%s: %d %s violations" % (corner, value, column.replace(" vios", "")))
        lines.append("%-22s" % corner + "".join("%13s" % cell for cell in cells))
    return lines, problems


def violators(report, limit=PINS_PER_SECTION):
    """The violator lists of a checks.rpt, worst first as OpenSTA writes them, at most `limit` pins in each."""
    lists, current = {}, None
    for line in report.splitlines():
        line = line.rstrip()
        if line.strip() in SECTIONS:
            current = line.strip()
            lists[current] = []
        elif line.startswith("====="):
            current = None
        elif current and re.search(r"\(VIOLATED\)$", line):
            lists[current].append(" ".join(line.split()[:-1]))
    out = []
    for section, pins in lists.items():
        out.append("  %s: %d listed (pin, limit, value, slack)" % (section, len(pins)))
        out += ["    " + pin for pin in pins[:limit]]
        if len(pins) > limit:
            out.append("    ... and %d more, in checks.rpt in the GDS_logs artifact" % (len(pins) - limit))
    return out


def spef_warnings(log):
    """How often STA could not match a name while reading the extracted parasitics."""
    return sum(1 for line in log.splitlines() if re.match(r"(Warning:|\[WARNING ).*\.spef line ", line))


def read_text(path):
    try:
        with open(path, encoding="utf8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def report(run_dir):
    """Every line to print, and the exit status."""
    lines = ["==== proteus layout report ===="]
    step = post_pnr_step(run_dir)
    # A step writes state_out.json only once it has succeeded.
    if not step or not isinstance(read_json(os.path.join(step, "state_out.json")), dict):
        lines.append("FAIL: no post-PNR timing figures in %s; the run stopped before or in that step." % run_dir)
        return lines, 1

    logged = {os.path.basename(os.path.dirname(path)) for path in glob(os.path.join(step, "*", "sta.log"))}
    corners = list(CORNERS) + sorted(logged - set(CORNERS))
    logs = {corner: read_text(os.path.join(step, corner, "sta.log")) or "" for corner in corners}
    lines.append("Extracted parasitics, from %s" % step)
    table, problems = figures(logs)
    lines += table

    final = read_json(os.path.join(run_dir, "final", "metrics.json"))
    if not isinstance(final, dict):
        lines.append("final/metrics.json: none, so the run stopped after the post-PNR STA")
        final = {}
    for key in SIGN_OFF:
        value = final.get(key)
        if not number(value):
            lines.append("%s: MISSING" % key)
            problems.append("no %s figure" % key)
            continue
        lines.append("%s: %s" % (key, value))
        if value != 0:
            problems.append("%s is %s" % (key, value))

    for corner in corners:
        lines.append("-- %s" % corner)
        checks = read_text(os.path.join(step, corner, "checks.rpt"))
        if checks is None:
            lines.append("  no checks.rpt")
            problems.append("%s: no checks.rpt" % corner)
        else:
            lines += violators(checks) or ["  no slew, cap or fanout violators"]
        lines.append("  parasitics warnings while reading SPEF: %d" % spef_warnings(logs[corner]))

    if problems:
        return lines + ["FAIL: " + problem for problem in problems], 1
    return lines + ["PASS: no slew, cap, setup or hold violations in any corner, nor routing, LVS or antenna ones"], 0


def main(argv):
    lines, status = report(argv[1] if len(argv) > 1 else os.path.join("runs", "wokwi"))
    print("\n".join(lines))
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
