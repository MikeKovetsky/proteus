# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The table of a learned SPI target, mode 0. The host writes it from the model, and from nothing else.

    out 0 MISO=P2    in A SCK=P0, B MOSI=P1, C CS=P3

One subroutine moves a byte for every state. It returns with J = 1 and the byte in S, or with J = 0 when CS
rose first. A state is then the call with the byte it sends, a branch on J, and its compare chain.

S is cleared where the machine waits for CS, so a waiting machine's registers hang on its state and little
else. The judge in learn/lab can then visit every one of them.

All three inputs are taken, so this clone cannot also watch nRESET. Resetting it is loading it again."""

from .bus import Refused
from .spi import DESELECT, HIGH, SELECT
from . import table as T

OUTS = (2, 0, 0, 0)
INS = (0, 1, 3)
MISO = 0
SCK, CS = T.A, T.C
J1 = 0x1000
K7 = 0x700
PIPELINE = 13  # clocks from a byte's last rising edge to the next one's first bit, beside the compare chain
CLOCK_KHZ = 60000


def generate(bus, model):
    t = T.Table()
    gap, head, entry, pre = {}, {}, {}, {}
    for q in range(model.n):  # state 0 waits for CS, so the table starts with it
        if model.phases[q] == HIGH:
            gap[q] = t.node(pin=(MISO, T.RELEASE), dst=T.TO_S, kj=T.J_LOAD, imm=J1)
    trap = t.node(pin=(MISO, T.RELEASE), dst=T.PUSH2, imm=0xEE)  # an input the host was told not to learn
    halt = t.node()
    t.go(trap, halt)
    t.go(halt, halt)

    edge = t.node(pin=(MISO, T.RELEASE), gate=T.GATE_IF)
    put = t.node(pin=(MISO, T.DATA), gate=T.GATE_UNLESS)
    rise = t.node(pin=(MISO, T.RELEASE), gate=T.GATE_IF)
    take = t.node(shift=1, din=T.DIN_B, kj=T.K_DEC)
    done = t.node(nop=T.RETURN)
    out = t.node(pin=(MISO, T.RELEASE), kj=T.J_LOAD, nop=T.RETURN)
    t.when(edge, (CS, SCK), (put, out, edge, out))   # SCK is low: the bit may change
    t.when(put, (CS,), (rise, out))
    t.when(rise, (CS, SCK), (rise, out, take, out))  # SCK rose
    t.when(take, (T.KZ,), (edge, done))

    for q in range(model.n):
        if model.phases[q] == HIGH:
            continue
        row = model.rows[q]
        byte = 0
        for s in model.alphabet:
            if s < 256 and s in row:
                byte = row[s][1]
                break
        if byte not in pre:
            pre[byte] = t.go(t.node(dst=T.TO_S, kj=T.K_LOAD, imm=K7 | byte), edge)
        head[q] = t.node()
        entry[q] = t.call(pre[byte], head[q])

    chains = T.Chains(t)
    for q in range(model.n):
        row = model.rows[q]
        if q in gap:
            to = row.get(SELECT)
            t.when(gap[q], (CS,), (entry[to[0]] if to else trap, gap[q]))
            continue
        to = row.get(DESELECT)
        left = gap[to[0]] if to else trap
        default = entry[row[model.defaults[0]][0]] if model.defaults else trap
        cases = [(s, entry[row[s][0]]) for s in model.alphabet if s < 256 and s in row]

        def then(first, node=head[q], left=left):
            t.when(node, (T.JZ,), (first, left))

        chains.want(cases, default, then)
    chains.build()

    period = CLOCK_KHZ // bus.serve
    if period < chains.longest + PIPELINE:
        raise Refused("too fast", "a clone with a chain of %d compares serves SPI up to %d kHz, not %d"
                      % (chains.longest, CLOCK_KHZ // (chains.longest + PIPELINE), bus.serve))
    packed = t.pack()
    return {
        "entries": packed.top,
        "chain": chains.longest,
        "image": packed.image(OUTS, INS) if packed.top <= T.DEPTH else None,
        "differs": [
            "MISO, its release and the answer to CS follow the pins through the chip's synchroniser and one or "
            "more entries, where the device had its own delay",
            "a deselect shorter than %d clocks can be missed" % (chains.longest + PIPELINE),
            "the clone has no input left for nRESET: to reset it, load it again",
        ],
    }
