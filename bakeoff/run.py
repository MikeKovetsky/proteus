"""Run every bench against every candidate and print the bake-off table.

    python3 -m bakeoff.run                         # every candidate, every bench
    python3 -m bakeoff.run --candidate cand_pio    # one candidate
    python3 -m bakeoff.run --bench uart_tx         # one bench
    python3 -m bakeoff.run --json                  # machine-readable
    python3 -m bakeoff.run --strict                # exit 1 unless every bench passes
"""
from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import sys
import traceback
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Optional

from .harness import benches as benches_pkg
from .harness.sim import Bench, Result

HERE = Path(__file__).resolve().parent


def discover_benches() -> List[Bench]:
    found: List[Bench] = []
    for module_info in sorted(pkgutil.iter_modules(benches_pkg.__path__), key=lambda m: m.name):
        module = importlib.import_module("%s.%s" % (benches_pkg.__name__, module_info.name))
        found.extend(getattr(module, "BENCHES", []))
    return found


def discover_candidates() -> List[str]:
    return sorted(p.name for p in HERE.glob("cand_*") if (p / "candidate.py").exists())


def select_benches(benches: List[Bench], names: Optional[List[str]]) -> List[Bench]:
    """The benches `names` asks for, in discovery order, or all of them when it is empty.

    A name that no bench has is an error, so a typo cannot leave nothing to run and `--strict` pass.
    """
    if not names:
        return benches
    unknown = sorted(set(names) - {bench.name for bench in benches})
    if unknown:
        raise ValueError("no bench named %s" % ", ".join(unknown))
    return [bench for bench in benches if bench.name in names]


def read_budget(module: ModuleType) -> Dict[str, object]:
    """The candidate's budget, or the error its `budget()` raised: one broken candidate does not stop the table."""
    try:
        return module.make_chip().budget()
    except Exception:
        return {"error": traceback.format_exc(limit=3).strip().splitlines()[-1]}


def run_candidate(name: str, benches: List[Bench]) -> Dict[str, object]:
    module = importlib.import_module("bakeoff.%s.candidate" % name)
    rows = []
    for bench in benches:
        try:
            image = module.program(bench.name)
        except Exception:  # a program that does not build fails its own bench, not the whole table
            error = traceback.format_exc(limit=3).strip().splitlines()[-1]
            rows.append({"bench": bench.name, "status": "fail", "detail": "build: " + error, "bytes": 0, "cycles": 0})
            continue
        if image is None:
            rows.append({"bench": bench.name, "status": "none", "detail": "no program", "bytes": 0, "cycles": 0})
            continue
        try:
            result = bench.run(module.make_chip, image)
        except Exception:  # a crash in a candidate is that candidate's failure, not the run's
            result = Result(bench.name, False, traceback.format_exc(limit=3).strip().splitlines()[-1])
        rows.append(
            {
                "bench": bench.name,
                "status": "pass" if result.passed else "fail",
                "detail": result.detail,
                "bytes": len(image),
                "cycles": result.cycles,
            }
        )
    return {"candidate": name, "budget": read_budget(module), "rows": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--candidate", action="append", help="a cand_* directory name; repeatable")
    parser.add_argument("--bench", action="append", help="a bench name; repeatable")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="exit 1 unless every bench passes")
    args = parser.parse_args()

    try:
        benches = select_benches(discover_benches(), args.bench)
    except ValueError as error:
        parser.error(str(error))
    candidates = args.candidate or discover_candidates()
    if not candidates:
        parser.error("no candidate: no bakeoff/cand_*/candidate.py exists")
    reports = [run_candidate(name, benches) for name in candidates]
    all_pass = all(row["status"] == "pass" for report in reports for row in report["rows"])
    exit_code = 1 if args.strict and not all_pass else 0

    if args.json:
        json.dump(reports, sys.stdout, indent=2, default=str)
        print()
        return exit_code

    for report in reports:
        rows = report["rows"]
        passed = sum(1 for r in rows if r["status"] == "pass")
        print("\n## %s: %d of %d benches pass" % (report["candidate"], passed, len(rows)))
        print("budget: %s\n" % json.dumps(report["budget"], default=str))
        print("| bench | status | program bytes | detail |")
        print("|---|---|---|---|")
        for r in rows:
            print("| %s | %s | %d | %s |" % (r["bench"], r["status"], r["bytes"], r["detail"]))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
