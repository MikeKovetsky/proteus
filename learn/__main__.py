# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""python3 -m learn: learn it, become it.

  python3 -m learn             the demonstration and the negative controls, in about five minutes
  python3 -m learn evaluate    the 160 seeded devices the host was not tuned on, every failure counted
  python3 -m learn baselines   the baselines in full, as long as the questions
  python3 -m learn measure     capacity and memory

All of it is simulation. It needs Verilator, Icarus and cocotb from the OSS CAD Suite:
    source ~/oss-cad-suite/environment"""

import argparse
import shutil
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .lab import board, cases, devices, families, judge, measure, passive, replace

JOBS = 3
SHORT = (1000, 10000)  # transactions recorded, in the demonstration
LONG = (100, 1000, 10000, 100000)
OVER_RATE = 5000  # kHz: the rate the SPI controller ships at
WIDTH = 118


def say(text=""):
    print(text, flush=True)


def wrapped(text, indent="", hang=None):
    say(textwrap.fill(text, WIDTH, initial_indent=indent, subsequent_indent=indent if hang is None else hang))


def heading(text):
    say()
    say(text)
    say("-" * len(text))


def seconds(ms):
    return "%.1f s" % (ms / 1000.0)


def learned(case):
    return case, cases.run(case)


def learn_all():
    every = cases.DEMONSTRATION + cases.CONTROLS
    done = {}
    with ThreadPoolExecutor(JOBS) as pool:
        for future in as_completed([pool.submit(learned, case) for case in every]):
            case, found = future.result()
            done[case.name] = found
            say("  %2d/%d  %-36s %s" % (len(done), len(every), case.name, found[1]["verdict"]))
    return done


def show_learning(done):
    heading("1. Learn it: the host asks through the chip's pins, and writes the chip's program")
    say("%-12s %6s %10s %9s %9s %8s  %s" % ("device", "states", "symbols", "chip", "here", "entries",
                                             "the judge, over every reachable state and symbol"))
    good = True
    for case in cases.DEMONSTRATION:
        result, report = done[case.name]
        good = good and report["expected"]
        if not result["ok"]:
            say("%-12s no program: %s" % (case.name, result["reason"]))
            continue
        stats = result["stats"]
        judged = ", ".join("%s at %d kHz (%d steps)" % ("equal" if found["equal"] and found["complete"] else
                                                      "NOT EQUAL", found["khz"], found["steps"])
                           for found in report["clone"])
        say("%-12s %6d %10d %9s %9s %4d/512  %s: %s" % (
            case.name, stats["states"], stats["symbols"], seconds(result["chip_ms"]),
            "%.0f s" % result["wall_s"], stats["entries"], report["verdict"], judged))
    for case in cases.DEMONSTRATION:
        say("  %s: %s" % (case.name, case.why))
    result = done[cases.DEMONSTRATION[0].name][0]
    for title, key in (("What the host had to be told or given", "needs"), ("What it guarantees", "guarantee"),
                       ("What it warns of", "warnings")):
        say()
        say("%s, for the flash:" % title)
        for i, line in enumerate(result[key], 1):
            wrapped("%d. %s" % (i, line), "  ", "     ")
    return good


def spread(least_most):
    least, most = int(round(least_most[0])), int(round(least_most[1]))
    return "%d" % least if least == most else "%d to %d" % (least, most)


def show_replacement(done):
    heading("2. Become it: the device is taken away and the shipped controller faces the clone, on two chips' RTL")
    good = True
    with replace.Pair() as pair:
        for case in cases.DEMONSTRATION:
            image = done[case.name][0]["image"]
            if image is None:
                good = False
                say("%s: no program to put in its place" % case.name)
                continue
            found = replace.replacement(pair, case.name, image, jobs=JOBS)
            good = good and found["replaced"] and not found["problems"]
            say("%s: %s over %d transactions" % (
                case.name, "replaced" if found["replaced"] else "NOT REPLACED", found["transactions"]))
            for key, value in found.items():
                if key.startswith(("test", "same")):
                    say("    %-52s %s" % (key, "yes" if value else "NO"))
                elif key.startswith("at 5 MHz"):
                    say("    outside its rate, %-34s %s" % (key + " that test", "yes" if value else "no"))
            for edge in sorted(found["delays"]["device"]):
                device, clone = (found["delays"][who].get(edge) or [0, 0] for who in ("device", "clone"))
                say("    where timing differs, after %-10s device %s ns, clone %s ns" % (
                    edge, spread(device), spread(clone)))
            for problem in found["problems"]:
                say("    problem: %s" % problem)
    return good


def show_limit(done):
    case = cases.DEMONSTRATION[0]
    image = done[case.name][0]["image"]
    if image is None:
        return False
    table = case.device()
    with board.Board(case.bus) as on:
        found = judge.clone(on, image, judge.Minimal(judge.TRUTH[case.bus](table)), OVER_RATE)
    wrapped("flash at %d kHz, past the rate the host was told it would serve: %s" % (
        OVER_RATE, "the judge finds no difference" if found["equal"] else
        "NOT the device, says the judge. It is too slow there, and a test that passes there doesn't show it."))
    return True


def baseline_row(part, length, learner, row):
    found, of = row["hidden features"]
    say("%-12s %-22s %9s %9s %7.1f%% %7d/%-3d %9s  %s" % (
        part, learner, length, row.get("symbols", ""), 100 * row["normal traffic"], row["device test"][0],
        row["device test"][1], "%d/%d" % (found, of) if of else "none", "exact" if row["exact"] else "no"))


def baseline_header():
    say("%-12s %-22s %9s %9s %8s %11s %10s %s" % ("device", "learner", "recorded", "symbols", "traffic",
                                                  "its test", "hidden", "exact"))


def show_baselines(done, lengths):
    heading("3. What questioning finds that a recording of normal traffic does not")
    baseline_header()
    for case in cases.DEMONSTRATION:
        result = done[case.name][0]
        if not result["model"]:
            continue
        found = passive.compared(case.name, case.device(), result["model"], lengths)
        baseline_row(case.name, "-", "questions (active)", dict(found["active"], symbols=result["stats"]["symbols"]))
        for length in lengths:
            for learner in ("passive", "states told"):
                if (length, learner) in found:
                    baseline_row(case.name, length, learner, found[(length, learner)])
    say("  passive: state merging, helped with the alphabet, the phases and the defaults. states told: a reference")
    say("  that is given the true state at each step. `python3 -m learn baselines` records as much as was asked.")


def show_controls(done):
    heading("4. The negative controls: what it must refuse, and what no test through the pins can find")
    good = True
    for case in cases.CONTROLS:
        result, report = done[case.name]
        good = good and report["expected"]
        say("%-4s %-36s %-15s %s" % ("ok" if report["expected"] else "BAD", case.name, report["verdict"],
                                     "" if result["code"] == "ok" else result["code"]))
        wrapped(case.why, "       ")
    return good


def demonstration():
    began = time.time()
    say("Learn it, become it. Simulation only: Verilated and Icarus RTL, devices made up for this, no hardware.")
    say("The host writes the program. The chip asks the questions and then runs what was written.")
    say()
    say("Learning %d devices and %d controls, %d at a time (about five minutes in all):"
        % (len(cases.DEMONSTRATION), len(cases.CONTROLS), JOBS))
    board.build()
    done = learn_all()
    good = show_learning(done)
    good = show_replacement(done) and good
    good = show_limit(done) and good
    show_baselines(done, SHORT)
    good = show_controls(done) and good
    say()
    say("%s in %.0f s" % ("All as it must be," if good else "NOT as it must be,", time.time() - began))
    return 0 if good else 1


def evaluation(out):
    if not families.frozen():
        say("learn/host or learn/programs is not what was frozen at %s, so this would be no evaluation."
            % families.FREEZE[:7])
        return 1
    if out:
        with open(out, "w"):
            pass
    began = time.time()
    rows = families.evaluate(out, say=say)
    heading("The host frozen at %s, on devices seeded from that hash" % families.FREEZE[:7])
    for line in families.summary(rows):
        say(line)
    say("%.0f s" % (time.time() - began))
    return 1 if any(row["verdict"] in judge.FAILURES for row in rows) else 0


def baselines():
    board.build()
    done = {case.name: cases.run(case) for case in cases.DEMONSTRATION}
    heading("The baselines in full")
    baseline_header()
    for case in cases.DEMONSTRATION:
        result, table = done[case.name][0], case.device()
        if not result["model"]:
            say("%s: no model to compare: %s" % (case.name, result["reason"]))
            return 1
        symbols = result["stats"]["symbols"]
        lengths = sorted(set(LONG + (passive.as_long(case.name, table, symbols),)))
        found = passive.compared(case.name, table, result["model"], lengths)
        baseline_row(case.name, "-", "questions (active)", dict(found["active"], symbols=symbols))
        for length in lengths:
            for learner in ("passive", "states told"):
                if (length, learner) in found:
                    baseline_row(case.name, length, learner, found[(length, learner)])
        for seed in (1, 2, 3):
            with board.Board(case.bus, seed) as on:
                on.plug(table)
                row = passive.questioned_at_random(case.name, table, on, dict(case.told, bus=case.bus, seed=seed),
                                                   symbols)
            if row.get("reason"):
                say("%-12s random questions %d: no model: %s" % (case.name, seed, row["reason"]))
            else:
                baseline_row(case.name, "-", "random questions %d" % seed, row)
    return 0


def measurements():
    board.build()
    heading("Capacity: the generator alone, every program that fits judged on RTL")
    good = True
    for row in measure.capacity():
        good = good and row["verdict"] not in judge.FAILURES
        each = "" if row["entries"] is None else "%.1f a state" % (row["entries"] / row["states"])
        say("%-32s %-12s %4d states %5s entries %-12s %s" % (
            row["name"], "inputs told" if row["inputs"] else "all bytes", row["states"], row["entries"], each,
            row["verdict"]))
    heading("The host's memory, in CPython")
    for case in cases.DEMONSTRATION:
        found = measure.memory(getattr(devices, case.name)(), case.told)
        good = good and found["ok"]
        say("%-12s %4d kB alive at most, over %d questions (%d kB with garbage not yet collected)" % (
            case.name, found["alive"] // 1000, found["queries"] or 0, found["peak"] // 1000))
    return 0 if good else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m learn", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", nargs="?", default="demo", choices=("demo", "evaluate", "baselines", "measure"))
    parser.add_argument("--out", help="evaluate: also write each device's row to this file, as JSON lines")
    asked = parser.parse_args(argv)
    missing = [tool for tool in ("verilator", "iverilog") if not shutil.which(tool)]
    if missing:
        say("%s not found. Run this first:  source ~/oss-cad-suite/environment" % " and ".join(missing))
        return 2
    if asked.what == "evaluate":
        return evaluation(asked.out)
    if asked.what == "baselines":
        return baselines()
    return measurements() if asked.what == "measure" else demonstration()


if __name__ == "__main__":
    sys.exit(main())
