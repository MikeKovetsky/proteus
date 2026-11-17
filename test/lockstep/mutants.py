"""Plant one bug at a time in a copy of the RTL and check that the lockstep suite catches every one.

    python3 test/lockstep/mutants.py            # after `make -C test/lockstep` has recorded the traces

A test suite that passes on a broken design proves nothing. Each mutant below is a small, plausible
mistake in src/ (an off-by-one, a swapped order, a missed corner of the model). The script copies src/
to build/mutants/NAME/, applies the one edit, builds the lockstep harness on it with Verilator and replays
every recorded trace. A mutant is killed when some trace mismatches. It exits 1 if any mutant survives or
if an edit no longer applies (the RTL changed under it).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BUILD = HERE / "build"

# (name, file, text in the RTL, replacement)
MUTANTS = [
    ("crc codes 6 and 7 keep the LFSR", "protoemu_core.v",
     "      default: crc_n = crc_step;", "      3'd6, 3'd7: crc_n = m_crc;\n      default: crc_n = crc_step;"),
    ("crc_ok compares the new LFSR", "protoemu_core.v",
     "r_crcok[m]      <= m_crc == m_match;", "r_crcok[m]      <= crc_n == m_match;"),
    ("ret ignores a link written in the same entry", "protoemu_core.v",
     "(f_nop == 2'd3) ? link_n :", "(f_nop == 2'd3) ? m_link :"),
    ("Nhi keeps the old low byte", "protoemu_core.v",
     "{bus, n1[7:0]}", "{bus, m_N[7:0]}"),
    ("pin ops in the wrong order", "protoemu_core.v",
     "      if (sel0[i]) oe_tmp = pin_op(f_p0op, oe_tmp[1], oe_tmp[0], d);\n"
     "      if (sel1[i]) oe_tmp = pin_op(f_p1op, oe_tmp[1], oe_tmp[0], d);",
     "      if (sel1[i]) oe_tmp = pin_op(f_p1op, oe_tmp[1], oe_tmp[0], d);\n"
     "      if (sel0[i]) oe_tmp = pin_op(f_p0op, oe_tmp[1], oe_tmp[0], d);"),
    ("open drain drives high", "protoemu_core.v",
     "3'd7:    pin_op = {1'b0, ~dd};", "3'd7:    pin_op = {dd, ~dd};"),
    ("gate code 3 acts like 0", "protoemu_core.v",
     "wire gate_ok = (f_gate == 2'd0) |", "wire gate_ok = (f_gate == 2'd0) | (f_gate == 2'd3) |"),
    ("previous A resets to 0", "protoemu_core.v",
     "r_preva  <= 4'b1111;", "r_preva  <= 4'b0000;"),
    ("timer wraps below zero", "protoemu_core.v",
     "(m_tz ? 16'd0 : m_T - 16'd1)", "(m_T - 16'd1)"),
    ("K load takes the J nibble", "protoemu_core.v",
     "k_load ? f_imm[11:8]", "k_load ? f_imm[15:12]"),
    ("pop from an empty FIFO returns its stale head", "protoemu_core.v",
     "3'd1:    bus = (cnt0 != 3'd0) ? head0 : 8'd0;", "3'd1:    bus = head0;"),
    ("host pop misses a byte pushed in the same clock", "protoemu_core.v",
     "assign h_has1  = (cnt1 != 3'd0) | acc1;", "assign h_has1  = (cnt1 != 3'd0);"),
    ("host room ignores this clock's pop", "protoemu_core.v",
     "assign h_room0 = (cnt0 != 3'd4) | m_pop0;", "assign h_room0 = (cnt0 != 3'd4);"),
    ("a push into a full FIFO is not flagged", "protoemu_core.v",
     "overflow <= overflow | (m_push1 & ~acc1) | (m_push2 & ~acc2);", "overflow <= overflow | (m_push2 & ~acc2);"),
    ("turns wrap one machine early", "protoemu_core.v",
     "wire [1:0] nxt      = (slot_inc >= nm) ? 2'd0 : slot_inc[1:0];",
     "wire [1:0] nxt      = (slot_inc >= nm - 3'd1) ? 2'd0 : slot_inc[1:0];"),
    ("every machine starts in entry 0", "protoemu_core.v",
     "r_st     <= {9'd3, 9'd2, 9'd1, 9'd0};", "r_st     <= 36'd0;"),
    ("first read after loading skips a machine", "protoemu_core.v",
     "wire [8:0] run_addr = (q_valid && nxt == slot) ? state_n : r_st[nxt*9+:9];",
     "wire [8:0] run_addr = (nxt == slot) ? state_n : r_st[nxt*9+:9];"),
    ("the loader does not clear the rest of the table", "protoemu_core.v",
     "assign ram_wen  = ld_ent | ld_fill;", "assign ram_wen  = ld_ent;"),
    ("input C reads input B's pin", "protoemu_core.v",
     "wire in_c = pins[m_imap[8:6]];", "wire in_c = pins[m_imap[5:3]];"),
    ("shift MSB-first enters at the top", "protoemu_core.v",
     "m_msb ? {m_S[6:0], bit_in} : {bit_in, m_S[7:1]}", "m_msb ? {bit_in, m_S[7:1]} : {bit_in, m_S[7:1]}"),
    ("the FIFO forgets a simultaneous push and pop", "protoemu_fifo.v",
     "cnt <= cnt + {2'b00, push} - {2'b00, pop};", "cnt <= (push & pop) ? cnt - 3'd1 : cnt + {2'b00, push} - {2'b00, pop};"),
]


def build(src: Path, out: Path) -> bool:
    rtl = [src / "protoemu_core.v", src / "protoemu_fifo.v", src / "protoemu_table.v"]
    sram = sorted((ROOT / "test" / "ihp_sram").glob("*.v"))
    (out / "obj").mkdir(parents=True, exist_ok=True)
    cmd = ["verilator", "--cc", "--exe", "--build", "-j", "0", "-O1", "--no-timing", "-Wno-STMTDLY",
           "--timescale", "1ns/1ps", "-DFUNCTIONAL", "--top-module", "protoemu_core",
           "--Mdir", str(out / "obj"), "-o", str(out / "lockstep"), str(ROOT / "test" / "lint" / "waivers.vlt")]
    cmd += [str(p) for p in rtl + sram] + [str(HERE / "lockstep.cpp")]
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def main() -> int:
    traces = sorted((BUILD / "traces").glob("*.trace"))
    if not traces:
        print("no traces: run `make -C test/lockstep` first")
        return 1
    survived = 0
    for index, (name, filename, old, new) in enumerate(MUTANTS):
        work = BUILD / "mutants" / ("m%02d" % index)
        shutil.rmtree(work, ignore_errors=True)
        shutil.copytree(ROOT / "src", work / "src")
        target = work / "src" / filename
        text = target.read_text()
        if text.count(old) != 1:
            print("STALE    %s: the edit no longer applies to %s" % (name, filename))
            survived += 1
            continue
        target.write_text(text.replace(old, new))
        if not build(work / "src", work):
            print("NOBUILD  %s" % name)
            survived += 1
            continue
        run = subprocess.run([str(work / "lockstep")] + [str(t) for t in traces], capture_output=True, text=True)
        if run.returncode != 0:
            first = next((line for line in run.stdout.splitlines() if line.startswith("FAIL ")), "")
            print("killed   %-48s %s" % (name, first[5:][:110]))
        else:
            print("SURVIVED %s" % name)
            survived += 1
    print("mutants: %d of %d killed" % (len(MUTANTS) - survived, len(MUTANTS)))
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
