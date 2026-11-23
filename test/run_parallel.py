#!/usr/bin/env python3
# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Parallel shard runner for the cocotb pin-level test suite (RTL or gate-level, Icarus or Verilator).

Builds the simulator once, runs balanced shards across CPU cores with zero overlap, verifies
every shard passes with no duplicate tests, and merges all shard JUnit XML files into
`test/results.xml` (compatible with `cocotb_tools.check_results` and GitHub's test-summary).
"""

import argparse
import os
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent

# Balanced shards covering 100% of test, test_scenarios, test_programs, and test_v2.
# Split by actual measured runtime so the slowest real-time tests (I2C 25ms timeout, PS/2,
# 9600-baud UART, 1-Wire) run concurrently across cores.
SHARDS = (
    ("i2c_timeouts", "test_programs", r"^test_programs\.(test_i2c_timeout_length|test_i2c_scl_stuck)"),
    ("ps2", "test_programs", r"^test_programs\.test_ps2_"),
    ("uart_onewire", "test_programs", r"^test_programs\.(test_uart_|test_onewire_)"),
    ("programs_rest", "test_programs",
     r"^test_programs\.(?!test_i2c_timeout_length|test_i2c_scl_stuck|test_ps2_|test_uart_|test_onewire_)"),
    ("core_v2", "test,test_scenarios,test_v2", ""),
)


def _run_cmd(cmd, env=None):
    run_env = dict(os.environ if env is None else env)
    run_env["PWD"] = str(TEST_DIR)
    return subprocess.run(cmd, cwd=str(TEST_DIR), env=run_env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)


def run_shard(name, modules, filt, sim, gates, out_xml):
    t0 = time.monotonic()
    cmd = [
        "make", "-f", "Makefile",
        f"SIM={sim}",
        f"GATES={gates}",
        f"COCOTB_TEST_MODULES={modules}",
        f"COCOTB_TEST_FILTER={shlex.quote(filt) if filt else ''}",
        f"COCOTB_RESULTS_FILE={out_xml}",
        "NODUMP=1",
        str(out_xml),
    ]
    res = _run_cmd(cmd)
    dt = time.monotonic() - t0
    return name, res.returncode, dt, res.stdout, out_xml


def merge_xml(shard_xmls, dest_xml):
    root = ET.Element("testsuites", name="results")
    merged_suite = ET.SubElement(root, "testsuite", name="all", package="all")
    executed = set()
    declared = set()
    total = failures = errors = 0
    total_time = 0.0

    for name, path in shard_xmls:
        if not path.exists():
            raise RuntimeError(f"shard {name!r} did not produce {path}")
        tree = ET.parse(str(path))
        cases = list(tree.iter("testcase"))
        if not cases:
            raise RuntimeError(f"shard {name!r} ran 0 testcases")
        ran_here = 0
        for tc in cases:
            key = (tc.attrib.get("classname", ""), tc.attrib.get("name", ""))
            declared.add(key)
            if tc.find("skipped") is not None:
                continue  # filtered out by COCOTB_TEST_FILTER in this shard
            if key in executed:
                raise RuntimeError(f"duplicate executed testcase across shards: {key}")
            executed.add(key)
            ran_here += 1
            total += 1
            total_time += float(tc.attrib.get("time", "0") or 0)
            if tc.find("failure") is not None:
                failures += 1
            if tc.find("error") is not None:
                errors += 1
            merged_suite.append(tc)
        if ran_here == 0:
            raise RuntimeError(f"shard {name!r} executed 0 non-skipped testcases")

    missing = declared - executed
    if missing:
        raise RuntimeError(f"testcases skipped in every shard: {sorted(missing)}")

    merged_suite.attrib["tests"] = str(total)
    merged_suite.attrib["failures"] = str(failures)
    merged_suite.attrib["errors"] = str(errors)
    merged_suite.attrib["skipped"] = "0"
    merged_suite.attrib["time"] = f"{total_time:.3f}"

    ET.ElementTree(root).write(str(dest_xml), encoding="utf-8", xml_declaration=True)
    return total, failures, errors, 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sim", default=os.environ.get("SIM", "icarus"))
    ap.add_argument("--gates", default=os.environ.get("GATES", "no"))
    ap.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 2))
    ap.add_argument("--out", default=str(TEST_DIR / "results.xml"))
    args = ap.parse_args()

    t_start = time.monotonic()
    print(f"[parallel] building {args.sim} (GATES={args.gates}, NODUMP=1)...", flush=True)
    build = _run_cmd(["make", "-f", "Makefile", f"SIM={args.sim}", f"GATES={args.gates}", "NODUMP=1", "build_sim"])
    if build.returncode != 0:
        print(build.stdout)
        sys.exit(build.returncode)
    print(f"[parallel] build finished in {time.monotonic() - t_start:.1f}s; running {len(SHARDS)} shards on {args.jobs} workers...",
          flush=True)

    shard_dir = TEST_DIR / "sim_build" / f"shards_{args.sim}_{args.gates}"
    shard_dir.mkdir(parents=True, exist_ok=True)
    for old in shard_dir.glob("*.xml"):
        old.unlink()

    failed_shards = []
    shard_xmls = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = []
        for name, modules, filt in SHARDS:
            out_xml = shard_dir / f"{name}.xml"
            shard_xmls.append((name, out_xml))
            futs.append(pool.submit(run_shard, name, modules, filt, args.sim, args.gates, out_xml))
        for fut in futs:
            name, rc, dt, out, out_xml = fut.result()
            status = "PASS" if rc == 0 else f"FAIL (rc={rc})"
            print(f"[parallel] shard {name:14s}: {status} ({dt:.1f}s)", flush=True)
            if rc != 0:
                failed_shards.append((name, out))

    for name, out in failed_shards:
        print(f"\n=== FAILED SHARD: {name} ===\n{out[-4000:]}")

    dest = Path(args.out)
    total, failures, errors, skips = merge_xml(shard_xmls, dest)
    wall = time.monotonic() - t_start
    print(f"[parallel] MERGED {dest.name}: TESTS={total} PASS={total - failures - errors - skips} "
          f"FAIL={failures} ERROR={errors} SKIP={skips} in {wall:.1f}s wall time", flush=True)

    if failed_shards or failures or errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
