"""The instruction set of the event machine: one table shared by the simulator and the assembler.

A word is 32 bits:

    [31:28] opcode   [27] invert the event   [26:22] event   [21:0] payload

Every instruction except BR waits until its event is true and its FIFO/CRC/shifter operands are ready, then
does its work in that one cycle. The next instruction can fire two cycles later (three after DJNZ).
"""

# Opcodes.
MOV, LDI, ALU, ACT, PIN, BR, DJNZ, JMP, RET, TRAP, HALT = range(11)

# Events (5 bits). An instruction waits on one; BR tests one; TRAP arms one.
EVENTS = {
    "always": 0,
    "p0": 1, "p1": 2, "p2": 3, "p3": 4, "p4": 5, "p5": 6, "p6": 7, "p7": 8,  # pin level is high
    "t0": 9, "t1": 10,          # timer expired (cleared when an instruction waiting on it fires)
    "rx.full": 11,              # the receive shifter has captured a word
    "tx.ready": 12,             # the transmit holding register is empty
    "tx.done": 13,              # the transmitter has sent its last bit and has nothing queued
    "match": 14,                # the pattern matcher matches
    "crc.busy": 15,
    "in0": 16, "in3": 17,       # host channel 0 / 3 has a byte
    "out1": 18, "out2": 19,     # host channel 1 / 2 has room
    "z": 20, "c": 21,           # flags of the last ALU instruction
    "smp.edge": 22,             # the sampler accepted an edge (one-cycle pulse)
    "smp.run": 23,              # the sampler's bit clock is running
    "rl.err": 24,               # a 1 arrived where the run-length unit expected the stuffed 0
    "sig0": 25, "sig1": 26,     # flags shared by both lanes
}
EV_P0, EV_T0, EV_T1 = 1, 9, 10
EV_RXFULL, EV_TXREADY, EV_TXDONE, EV_MATCH, EV_CRCBUSY = 11, 12, 13, 14, 15
EV_IN0, EV_IN3, EV_OUT1, EV_OUT2, EV_Z, EV_C = 16, 17, 18, 19, 20, 21
EV_SMPEDGE, EV_SMPRUN, EV_RLERR, EV_SIG0, EV_SIG1 = 22, 23, 24, 25, 26

# Sources of a move (4 bits).
SOURCES = {
    "r0": 0, "r1": 1, "r2": 2, "r3": 3,
    "fifo0": 4, "fifo3": 5,     # pop a byte from the host; waits while empty
    "rx": 6,                    # the captured receive word; waits for rx.full and clears it
    "crc.lo": 7, "crc.hi": 8,   # the CRC register (after the output inversion); waits while busy
    "pins": 9,                  # the eight pin levels
    "t0": 10, "t1": 11,         # a timer's running count, for measuring pulse widths
}
S_FIFO0, S_FIFO3, S_RX, S_CRCLO, S_CRCHI, S_PINS, S_T0, S_T1 = 4, 5, 6, 7, 8, 9, 10, 11

# Destinations of a move (5 bits). 16 and up are configuration registers.
DESTS = {
    "none": 0,
    "r0": 1, "r1": 2, "r2": 3, "r3": 4,
    "fifo1": 5, "fifo2": 6,     # push a byte to the host; waits while full
    "tx": 7,                    # the transmit holding register; waits while it is full
    "crc": 8,                   # feed one byte to the CRC unit (eight cycles); waits while busy
    "crc.lo": 9, "crc.hi": 10,  # write the CRC register directly (the initial value)
    "t0": 11, "t1": 12,         # set a timer's period and start it
    "smp.period": 16, "smp.phase": 17, "smp.cfg": 18,
    "rx.cfg": 19, "tx.cfg": 20, "rl.n": 21,
    "crc.poly.lo": 22, "crc.poly.hi": 23, "crc.cfg": 24,
    "match.val": 25, "match.mask": 26, "match.cfg": 27,
    "tmr.cfg": 28, "pins.src": 29, "pins.od": 30,
}
D_R0, D_FIFO1, D_FIFO2, D_TX, D_CRC, D_CRCLO, D_CRCHI, D_T0, D_T1 = 1, 5, 6, 7, 8, 9, 10, 11, 12
D_SMPPERIOD, D_SMPPHASE, D_SMPCFG, D_RXCFG, D_TXCFG, D_RLN = 16, 17, 18, 19, 20, 21
D_POLYLO, D_POLYHI, D_CRCCFG, D_MVAL, D_MMASK, D_MCFG, D_TMRCFG, D_PINSRC, D_PINOD = 22, 23, 24, 25, 26, 27, 28, 29, 30

# ALU functions (3 bits). CMP and TST set the flags and write nothing.
ALU_FN = {"add": 0, "sub": 1, "and": 2, "or": 3, "xor": 4, "shr": 5, "cmp": 6, "tst": 7}

# Strobes of ACT (one bit each). They reach the helpers one cycle after the instruction fires.
STROBES = {
    "tx.tick": 0,     # one bit time for a transmitter clocked by the program (tx.cfg ext)
    "tx.flush": 1,    # drop what the transmitter holds and go idle
    "rx.shift": 2,    # sample the data pin into the receive shifter now
    "rx.clear": 3,    # empty the receive shifter and restart its bit count (byte alignment)
    "smp.arm": 4,     # the sampler waits for an edge
    "smp.stop": 5,
    "rl.clear": 6,
    "t0.stop": 7, "t1.stop": 8,
    "sig0.set": 9, "sig0.clr": 10, "sig1.set": 11, "sig1.clr": 12,
}

# Bit fields of the configuration registers: name -> (shift, width, {symbol: value} or None for a number).
PINSEL = {"p%d" % i: i for i in range(8)}
FIELDS = {
    "smp": ("smp.cfg", {
        "clk": (0, 3, PINSEL),                                  # the pin whose edges are tracked
        "data": (3, 3, PINSEL),                                 # the pin that is sampled
        "edge": (6, 2, {"any": 0, "fall": 1, "rise": 2}),
        "mode": (8, 2, {"free": 0, "gate": 1, "once": 2}),      # see README: resync, one tick per edge, one frame
        "invert": (10, 1, None),                                # invert the sampled level
        "nrzi": (11, 1, None),                                  # bit = 1 when the level did not change
    }),
    "rx": ("rx.cfg", {
        "nbits": (0, 4, "minus1"),
        "msb": (4, 1, None), "lsb": (4, 0, None),
        "clock": (5, 2, {"smp": 0, "mid": 1, "end": 2, "manual": 3}),
        "destuff": (7, 1, None),
    }),
    "tx": ("tx.cfg", {
        "nbits": (0, 4, "minus1"),
        "msb": (4, 1, None), "lsb": (4, 0, None),
        "code": (5, 2, {"nrz": 0, "nrzi": 1, "manch": 2}),
        "invert": (7, 1, None),
        "split": (8, 1, None),                                  # two ticks per bit: Manchester halves, or a clock pin
        "stuff": (9, 1, None),
        "init": (10, 1, None),                                  # the line level an NRZI stream starts from
        "clkinv": (11, 1, None),
        "ext": (12, 1, None),                                   # clocked by `act tx.tick`, not by timer t0
    }),
    "crc": ("crc.cfg", {"reflect": (0, 1, None), "xorout": (1, 1, None)}),
    "match": ("match.cfg", {"src": (0, 1, {"rx": 0, "pins": 1})}),
    "tmr": ("tmr.cfg", {
        "t0.auto": (0, 1, None), "t0.retrig": (1, 6, "event"),
        "t1.auto": (8, 1, None), "t1.retrig": (9, 6, "event"),
    }),
}
PIN_SRC = {"tx": 1, "~tx": 2, "clk": 3}
PIN_OP = {"0": 1, "1": 2, "z": 3}

MEM_WORDS = 512
N_LANES = 2
FIFO_DEPTH = 4
