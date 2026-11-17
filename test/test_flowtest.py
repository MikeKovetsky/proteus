# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
#
# Tests of the flow test (src/flowtest_top.v), on RTL and on the gate-level netlist. The real RTL's tests
# replace this file. A Python model below computes what the chip must show: the self-test result, the walk's
# signature and the last filler lane, so every flop in the design is checked through the outputs.

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge

M64 = (1 << 64) - 1
SEED = 0x0123_4567_89AB_CDEF
TAPS = 0xD800_0000_0000_0000
ENTRIES = 512
WALK_STEPS = 1024
LANES = 16
EVEN = 0x5555_5555_5555_5555

# Rising edges after reset is released until a flag shows on uo_out: one idle clock, 512 writes, 512 reads,
# then the last compare in the first walk clock (self-test done) or 1024 walk clocks and one more to take the
# last word (walk done). The flag is registered, so it shows after the edge that ends its clock.
EDGES_TO_SELFTEST_DONE = 1 + ENTRIES + ENTRIES + 1
EDGES_TO_WALK_DONE = 1 + ENTRIES + ENTRIES + WALK_STEPS + 1


def lfsr_next(x):
    return (x >> 1) ^ (TAPS if x & 1 else 0)


def rotl(x, r):
    return ((x << r) | (x >> (64 - r))) & M64


def planted_faults():
    """Entry 32j+5 gets bit 4j+1 flipped, j = 0..15, when ui_in[3:0] is 15 during the writes."""
    return {32 * j + 5: 1 << (4 * j + 1) for j in range(16)}


def model(cond_nibble, faults=None):
    """The walk's signature and the last lane after the walk, as flowtest_top.v computes them."""
    mem = []
    x = SEED
    for addr in range(ENTRIES):
        mem.append(x ^ (faults or {}).get(addr, 0))
        x = lfsr_next(x)

    lanes = [0] * LANES

    def take(word):  # flowtest_lanes.v: one clock with en high
        old = lanes[:]
        for i in range(LANES):
            inp = word if i == 0 else old[i - 1] & EVEN
            lanes[i] = rotl(old[i], 2 * i + 1) ^ inp

    for addr in range(ENTRIES):  # the self-test reads, in address order
        take(mem[addr])

    sig = 0
    dout = mem[ENTRIES - 1]  # the walk starts from the last word the self-test read
    for step in range(WALK_STEPS):
        cond = (cond_nibble << 12) | ((step & 0x3FF) << 2) | (sig & 3)
        c_a = (cond >> ((dout >> 9) & 0xF)) & 1
        c_b = (cond >> ((dout >> 13) & 0xF)) & 1
        addr = (dout & 0x1FF) | c_a | (c_b << 1)
        if step > 0:  # the word read by the previous walk clock
            sig = rotl(sig, 1) ^ dout
            take(dout)
        dout = mem[addr]
    sig = rotl(sig, 1) ^ dout  # the last walk word, taken in the first idle clock
    take(dout)
    return sig, lanes[-1]


def status(dut):
    v = int(dut.uo_out.value)
    return {
        "done": v & 1,
        "pass": (v >> 1) & 1,
        "fail": (v >> 2) & 1,
        "walk_done": (v >> 3) & 1,
        "mismatches": v >> 4,
    }


async def reset(dut, ui):
    dut.ena.value = 1
    dut.ui_in.value = ui
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    await FallingEdge(dut.clk)  # release reset away from a rising edge
    dut.rst_n.value = 1


def check_sram_ties(dut):
    """The datasheet's tie-off rule: A_DLY is high on both macros. The PDK model checks it too, but it only
    prints an error and calls $stop, which the simulator runs past under cocotb, so the test checks it here.
    The macros keep their instance names (sram_a, sram_b) in the gate-level netlist too."""
    for name in ("sram_a", "sram_b"):
        dly = getattr(dut.user_project, name).A_DLY.value
        assert str(dly) == "1", f"{name}.A_DLY is {dly}; the datasheet says it must be tied to 1"


async def edges_until(dut, flag, limit):
    """Rising edges until the flag shows on uo_out, checked just after each edge."""
    for edge in range(1, limit + 1):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        if status(dut)[flag]:
            return edge
    raise AssertionError(f"{flag} did not show within {limit} clocks; uo_out is {status(dut)}")


async def read_bytes(dut, cond_nibble):
    """All 16 bytes on uio_out: 0-7 the signature, 8-15 the last lane. Call it on a falling edge."""
    out = []
    # The read-out register lags the signature by a clock: let it take the final value first.
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    prev = int(dut.uio_out.value)
    for sel in range(16):
        dut.ui_in.value = (cond_nibble << 4) | sel
        # Two synchroniser flops and the read-out register: the new byte shows after exactly three clocks.
        await ClockCycles(dut.clk, 2)
        await FallingEdge(dut.clk)
        early = int(dut.uio_out.value)
        assert early == prev, f"byte {sel} showed after 2 clocks; the input synchroniser should take 2 and the read-out 1"
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        prev = int(dut.uio_out.value)
        out.append(prev)
    sig = sum(b << (8 * i) for i, b in enumerate(out[:8]))
    lane = sum(b << (8 * i) for i, b in enumerate(out[8:]))
    return sig, lane


async def run_and_check(dut, cond_nibble, plant_faults=False):
    faults = planted_faults() if plant_faults else None
    want_sig, want_lane = model(cond_nibble, faults)
    # 16 faulty entries: the count saturates at 15
    want_result = {"pass": 0, "fail": 1, "mismatches": 15} if faults else {"pass": 1, "fail": 0, "mismatches": 0}

    await reset(dut, (cond_nibble << 4) | (0xF if plant_faults else 0))
    s = status(dut)
    assert s == {"done": 0, "pass": 0, "fail": 0, "walk_done": 0, "mismatches": 0}, f"after reset: {s}"
    assert int(dut.uio_oe.value) == 0xFF, f"uio_oe is {dut.uio_oe.value}, expected all outputs"
    check_sram_ties(dut)

    edges = await edges_until(dut, "done", EDGES_TO_WALK_DONE)
    assert edges == EDGES_TO_SELFTEST_DONE, f"self-test done after {edges} clocks, expected {EDGES_TO_SELFTEST_DONE}"
    s = status(dut)
    assert {k: s[k] for k in want_result} == want_result, f"self-test: {s}, expected {want_result}"
    assert s["walk_done"] == 0, f"walk done too early: {s}"

    edges += await edges_until(dut, "walk_done", EDGES_TO_WALK_DONE)
    assert edges == EDGES_TO_WALK_DONE, f"walk done after {edges} clocks, expected {EDGES_TO_WALK_DONE}"

    sig, lane = await read_bytes(dut, cond_nibble)
    assert sig == want_sig, f"walk signature {sig:016x}, expected {want_sig:016x}"
    assert lane == want_lane, f"last lane {lane:016x}, expected {want_lane:016x}"

    s = status(dut)
    assert s == {"done": 1, "walk_done": 1, **want_result}, f"at the end: {s}"
    assert int(dut.uio_oe.value) == 0xFF, f"uio_oe is {dut.uio_oe.value}, expected all outputs"


def start_clock(dut):
    # 60 MHz is 16.67 ns; a whole number of ns keeps the simulator's time exact. Gate-level runs are zero-delay.
    cocotb.start_soon(Clock(dut.clk, 16, unit="ns").start())


@cocotb.test()
async def test_selftest_and_walk_conditions_low(dut):
    """Self-test passes, and the walk with all four condition inputs low gives the model's signature."""
    start_clock(dut)
    await run_and_check(dut, 0x0)


@cocotb.test()
async def test_selftest_and_walk_conditions_mixed(dut):
    """The same with condition inputs 1010, which sends the walk down other paths."""
    start_clock(dut)
    await run_and_check(dut, 0xA)


@cocotb.test()
async def test_planted_faults_fail_the_selftest(dut):
    """16 entries written with one bit flipped: the self-test fails, counts 15 (saturated), and the walk
    still matches the model of the faulty table."""
    start_clock(dut)
    await run_and_check(dut, 0x6, plant_faults=True)


@cocotb.test()
async def test_reset_mid_walk_restarts(dut):
    """A reset in the middle of the walk restarts the loader, which rewrites and re-checks the whole table."""
    start_clock(dut)
    await reset(dut, 0xF << 4)
    await ClockCycles(dut.clk, EDGES_TO_SELFTEST_DONE + WALK_STEPS // 2)
    s = status(dut)
    assert s["done"] == 1 and s["walk_done"] == 0, f"before the second reset: {s}"
    await run_and_check(dut, 0xF)
