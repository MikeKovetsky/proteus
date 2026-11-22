#!/usr/bin/env python3
# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""
Prints a layout run's sign-off timing and electrical figures in full, and fails on a violation.

    python3 .github/scripts/layout_report.py [runs/wokwi]

LibreLane's own table is cut at 80 columns in CI's log (a count reads "1…"), and the violating pins are only
in report files inside an artifact. This reads both from the post-PNR STA step, OpenROAD.STAPostPNR, and from
nowhere else: the mid-PNR steps write the same metric names from estimated parasitics, and
final/metrics.json does not exist when a run stops early. So it fails when that step left no figures.

Failing: max slew, max cap, setup and hold violations in any corner, or a missing figure. Max fanout is
printed and does not fail.
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
# Printed for the reader. LibreLane's own checkers and the precheck judge these.
OTHER = (
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


def figures(metrics):
    """The table's lines, and what is wrong with the figures."""
    found = {key.split("__corner:", 1)[1] for key in metrics if key.startswith(COLUMNS[0][1] + "__corner:")}
    corners = list(CORNERS) + sorted(found - set(CORNERS))
    lines = ["%-22s" % "corner" + "".join("%13s" % column for column, _, _ in COLUMNS)]
    problems = []
    for corner in corners:
        cells = []
        for column, metric, fails in COLUMNS:
            value = metrics.get("%s__corner:%s" % (metric, corner))
            if not number(value):
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
    lines = ["==== protoemu layout report ===="]
    step = post_pnr_step(run_dir)
    state = read_json(os.path.join(step, "state_out.json")) if step else None
    metrics = state.get("metrics") if isinstance(state, dict) else None
    if not isinstance(metrics, dict):
        lines.append("FAIL: no post-PNR timing figures in %s; the run stopped before or in that step." % run_dir)
        return lines, 1

    lines.append("Extracted parasitics, from %s" % step)
    table, problems = figures(metrics)
    lines += table

    final = read_json(os.path.join(run_dir, "final", "metrics.json"))
    if isinstance(final, dict):
        lines += ["%s: %s" % (key, final.get(key, "not reported")) for key in OTHER]
    else:
        lines.append("final/metrics.json: none, so the run stopped after the post-PNR STA")

    for corner in CORNERS:
        lines.append("-- %s" % corner)
        checks = read_text(os.path.join(step, corner, "checks.rpt"))
        if checks is None:
            lines.append("  no checks.rpt")
            problems.append("%s: no checks.rpt" % corner)
        else:
            lines += violators(checks) or ["  no slew, cap or fanout violators"]
        log = read_text(os.path.join(step, corner, "sta.log"))
        if log is not None:
            lines.append("  parasitics warnings while reading SPEF: %d" % spef_warnings(log))

    if problems:
        return lines + ["FAIL: " + problem for problem in problems], 1
    return lines + ["PASS: no slew, cap, setup or hold violations in any corner"], 0


def main(argv):
    lines, status = report(argv[1] if len(argv) > 1 else os.path.join("runs", "wokwi"))
    print("\n".join(lines))
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
