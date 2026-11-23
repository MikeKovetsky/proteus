# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The table of a learned I2C target. The host writes it from the model, and from nothing else.

    out 0 SDA=P1 (open drain)    in A SCL=P0, B SDA=P1, C nRESET=P4

Three subroutines serve every state. `watch` waits for START or STOP. `take` receives a byte, and `ack` is the
same after an ACK. `send` is a byte out and the controller's ACK. The first three return
    J=1 K=1  a byte is in S        J=1 K=0  a STOP        J=0 K=1  a START
so a state is a call and one four-way branch on K and J.

S is cleared where the machine waits, so a waiting machine's registers hang on its state and little else. The
judge in learn/lab can then visit every one of them.

Every wait that can last also watches nRESET. SDA changes about a third of a microsecond after SCL falls."""

from .bus import Refused
from .i2c import ACK, ADDRESS_R, ADDRESS_W, DATA, IDLE, NACK, READ, START, STOP, WRITE
from . import table as T

OUTS = (1, 0, 0, 0)
INS = (0, 1, 4)
SDA_OUT = 0
SCL, SDA, NRESET = T.A, T.B, T.C
J1, K1, K7, K8 = 0x1000, 0x100, 0x700, 0x800
HOLD = 14      # clocks from seeing SCL fall to letting SDA change
R_OR_W = 9     # clocks to tell a read address from a write one, where their defaults differ
LONGEST = ((100, 160), (400, 38))  # compares that fit between SCL falling and the ACK being due


def subroutines(t, boot):
    def guarded(node, low, high):
        """A wait on SCL that reset can break."""
        return t.when(node, (NRESET, SCL), (boot, low, boot, high))

    def live(node, then):
        return t.when(node, (NRESET,), (boot, then))

    starts = t.node(kj=T.J_LOAD)
    stops = t.node(kj=T.K_LOAD, nop=T.RETURN)
    t.go(starts, t.node(kj=T.K_LOAD, imm=K1, nop=T.RETURN))

    # watch, for START or STOP
    watch = t.node(dst=T.TO_S)
    look, level, was_low, low_again, was_high, high_again = [t.node() for _ in range(6)]
    t.go(watch, look)
    guarded(look, look, level)
    t.when(level, (SDA,), (was_low, was_high))
    t.when(was_high, (SCL, SDA), (look, starts, look, high_again))
    t.when(was_low, (SCL, SDA), (look, low_again, look, stops))
    live(high_again, was_high)
    live(low_again, was_low)

    # take, entered at a START
    take = t.node(dst=T.TO_S, kj=T.J_LOAD, imm=J1)
    start_ends, bit, zero, zero_again, one, one_again = [t.node() for _ in range(6)]
    count = t.node(kj=T.K_LOAD, imm=K8)
    sample = t.node(shift=1, din=T.DIN_B, kj=T.K_DEC)
    fell = t.node(tload=1, imm=HOLD)
    t.go(take, start_ends)
    guarded(start_ends, count, start_ends)
    t.go(count, bit)
    guarded(bit, bit, sample)
    t.when(sample, (SDA,), (zero, one))
    t.when(one, (SCL, SDA), (fell, starts, fell, one_again))
    t.when(zero, (SCL, SDA), (fell, zero_again, fell, stops))
    live(one_again, one)
    live(zero_again, zero)
    t.when(fell, (T.KZ,), (bit, t.node(kj=T.K_LOAD, imm=K1, nop=T.RETURN)))

    # ack, then take
    ack, ack_rise, ack_fall, ack_wait = [t.node() for _ in range(4)]
    pull = t.node(pin=(SDA_OUT, T.LOW), dst=T.TO_S)
    hold = t.node(tload=1, imm=HOLD)
    let_go = t.node(pin=(SDA_OUT, T.RELEASE), kj=T.K_LOAD, imm=K8)
    t.when(ack, (T.TZ,), (ack, pull))
    t.go(pull, ack_rise)
    guarded(ack_rise, ack_rise, ack_fall)
    guarded(ack_fall, hold, ack_fall)
    t.go(hold, ack_wait)
    t.when(ack_wait, (T.TZ,), (ack_wait, let_go))
    t.go(let_go, bit)

    # send, with the byte in S and K = 7. SCL is low after an address and high after a controller's ACK
    send, before_ack, acked, high, wait, next_bit, last_rise, last_fall, last_wait, ninth = [
        t.node() for _ in range(10)]
    pull_first = t.node(pin=(SDA_OUT, T.LOW))
    fall = t.node(tload=1, imm=HOLD)
    drive = t.node(pin=(SDA_OUT, T.OPEN_DRAIN), shift=1, din=T.DIN_1, kj=T.K_DEC)
    last_hold = t.node(tload=1, imm=HOLD)
    release = t.node(pin=(SDA_OUT, T.RELEASE))
    sent = t.node(nop=T.RETURN)
    t.when(send, (SCL,), (before_ack, high))
    t.when(before_ack, (T.TZ,), (before_ack, pull_first))
    t.go(pull_first, acked)
    guarded(acked, acked, high)
    guarded(high, fall, high)
    t.go(fall, wait)
    t.when(wait, (T.TZ,), (wait, drive))
    t.when(drive, (T.KZ,), (next_bit, last_rise))
    guarded(next_bit, next_bit, high)
    guarded(last_rise, last_rise, last_fall)
    guarded(last_fall, last_hold, last_fall)
    t.go(last_hold, last_wait)
    t.when(last_wait, (T.TZ,), (last_wait, release))
    t.go(release, ninth)
    guarded(ninth, ninth, sent)
    return watch, take, ack, send


def generate(bus, model):
    t = T.Table()
    boot = t.node(pin=(SDA_OUT, T.RELEASE), dst=T.TO_S, kj=T.J_LOAD, imm=J1)
    waiting = t.node()
    trap = t.node(pin=(SDA_OUT, T.RELEASE), dst=T.PUSH2, imm=0xEE)  # an input the host was told not to learn
    halt = t.node()
    t.go(boot, waiting)
    t.go(trap, halt)
    t.go(halt, halt)
    watch, take, ack, send = subroutines(t, boot)

    head, entry, readdressed, pre = {}, {}, {}, {}
    for q in range(model.n):
        head[q] = t.node()
        phase = model.phases[q]
        if phase == READ:
            row = model.rows[q]
            found = row.get(ACK) or row.get(NACK)
            if found is None:
                entry[q] = trap
                continue
            byte = found[1]
            if byte not in pre:
                pre[byte] = t.go(t.node(dst=T.TO_S, kj=T.K_LOAD, imm=K7 | byte), send)
            entry[q] = t.call(pre[byte], head[q])
        elif phase == WRITE:
            entry[q] = t.call(ack, head[q])
            readdressed[q] = t.node()
        else:
            entry[q] = t.call(watch, head[q])
            readdressed[q] = head[q]
    t.when(waiting, (NRESET,), (waiting, entry[0]))

    def target(row, s):
        found = row.get(s)
        return entry[found[0]] if found else trap

    chains = T.Chains(t)
    split = {}
    for q in range(model.n):
        row, phase = model.rows[q], model.phases[q]
        if phase == READ:
            t.when(head[q], (SDA,), (target(row, ACK), target(row, NACK)))
            continue
        stop = entry[q] if phase == IDLE else target(row, STOP)
        again = t.call(take, readdressed[q])
        reads = writes = trap
        if model.defaults:
            writes = target(row, model.defaults[ADDRESS_W])
            reads = target(row, model.defaults[ADDRESS_R])
        default = writes
        if reads != writes:
            if (writes, reads) not in split:
                begin = t.node(kj=T.K_LOAD, imm=0x600)
                shift = t.node(shift=1, din=T.DIN_0, kj=T.K_DEC)
                tell = t.node()
                t.go(begin, shift)
                t.when(shift, (T.KZ,), (shift, tell))
                t.when(tell, (T.D,), (writes, reads))
                split[(writes, reads)] = begin
            default = split[(writes, reads)]
        cases = []
        for s in model.alphabet:
            if START <= s < ACK and s in row:
                to = entry[row[s][0]]
                usual = reads if s & 1 else writes
                if to != usual or not model.defaults:
                    cases.append((s - START, to))

        def addressed(first, node=readdressed[q], stop=stop, again=again):
            t.when(node, (T.KZ, T.JZ), (first, stop, again, None))

        chains.want(cases, default, addressed)
        if phase == WRITE:
            usual = target(row, model.defaults[DATA]) if model.defaults else trap
            cases = [(s, entry[row[s][0]]) for s in model.alphabet if s < START and s in row]

            def written(first, node=head[q], stop=stop, again=again):
                t.when(node, (T.KZ, T.JZ), (first, stop, again, None))

            chains.want(cases, usual, written)
    chains.build()

    slow = chains.longest + (R_OR_W if split else 0)
    room = None
    for khz, most in LONGEST:
        if bus.serve <= khz:
            room = most
            break
    if room is None:
        raise Refused("too fast", "a clone serves I2C up to 400 kHz, not %d" % bus.serve)
    if slow > room:
        raise Refused("too fast", "the longest decision takes %d clocks, and %d fit at %d kHz"
                      % (slow, room, bus.serve))
    packed = t.pack()
    return {
        "entries": packed.top,
        "chain": chains.longest,
        "image": packed.image(OUTS, INS) if packed.top <= T.DEPTH else None,
        "differs": [
            "SDA changes a fixed time after SCL falls, where the device had its own delay",
            "the clone never stretches the clock",
            "a START or STOP in the middle of a byte being read is not followed",
        ],
    }
