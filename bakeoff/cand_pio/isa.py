"""Instruction and configuration encoding shared by the assembler and the simulator.

A word is 32 bits:  [31:28] op  [27] T  [26] side_en  [25:24] side  [23:16] delay  [15:0] arg
T makes the instruction start on the machine's next timer tick. For WAITT, T means "time out on the tick".
"""

# Opcodes.
JMP, JPIN, WAIT, WAITT, IN, OUT, PUSH, PULL, MOV, SET, CTL = range(11)

# JMP conditions.
C_ALWAYS, C_NX, C_XDEC, C_NY, C_YDEC, C_XNEY, C_NOSRE, C_NISRE, C_CRCOK, C_NCRCOK = range(10)
C_CALL, C_RET = 14, 15
CONDS = {"!x": C_NX, "x--": C_XDEC, "!y": C_NY, "y--": C_YDEC, "x!=y": C_XNEY, "!osre": C_NOSRE,
         "!isre": C_NISRE, "crcok": C_CRCOK, "!crcok": C_NCRCOK}

# IN sources and OUT destinations.
S_PINS, S_X, S_Y, S_NULL, S_ONES, S_OSR = range(6)
D_PINS, D_X, D_Y, D_NULL, D_DIRS = range(5)

# MOV registers.
R_X, R_Y, R_ISR, R_OSR, R_NULL, R_PINS, R_CRC = range(7)
MOV_REGS = {"x": R_X, "y": R_Y, "isr": R_ISR, "osr": R_OSR, "null": R_NULL, "pins": R_PINS, "crc": R_CRC}

# SET destinations.
T_PINS, T_X, T_Y, T_DIRS, T_TOGGLE = range(5)

# CTL sub-operations.
K_NOP, K_CRCINIT, K_TRESTART, K_THALF = range(4)
CRC_INITS = [0x00000000, 0x000000FF, 0x0000FFFF, 0xFFFFFFFF]

# PUSH sources.
P_ISR, P_X, P_Y, P_IMM = range(4)

# Configuration registers, written by the loader.
CFG_PADS, CFG_POLY, CFG_RESIDUE = 0, 1, 2   # PADS: out[7:0] oe[15:8] open_drain[23:16] crc_tap[27:24]
CFG_TIMER0, CFG_CTRL0 = 4, 5                # machine k: TIMER at 4+2k, CTRL at 5+2k
# TIMER: frac[7:0] int[23:8].  CTRL: enable[0] start[9:1] in_left[10] out_left[11] autopush[12] autopull[13]
#        push_ch2[14] pull_ch3[15] side_base[19:16] side_two[20] side_dirs[21]

N_SM = 4
MEM_WORDS = 512


def word(op, arg, t=0, side=None, delay=0):
    w = (op << 28) | (t << 27) | ((delay & 0xFF) << 16) | (arg & 0xFFFF)
    if side is not None:
        w |= (1 << 26) | ((side & 3) << 24)
    return w


def decode(w):
    """Split a word into (op, t, side_en, side, delay, a, b, c, d). The fields a..d depend on the op."""
    op = (w >> 28) & 15
    t, se, sv, dl, arg = (w >> 27) & 1, (w >> 26) & 1, (w >> 24) & 3, (w >> 16) & 0xFF, w & 0xFFFF
    a = b = c = d = 0
    if op == JMP:
        a, b = arg >> 12, arg & 0x1FF
    elif op in (JPIN, WAIT, WAITT):
        a, b, c = arg >> 15, (arg >> 11) & 15, arg & 0x1FF
    elif op in (IN, OUT):
        a, b, c = arg >> 13, (arg >> 7) & 63, arg & 15
    elif op == PUSH:
        a, b, c, d = 2 if arg >> 15 else 1, (arg >> 14) & 1, (arg >> 12) & 3, arg & 0xFF
    elif op == PULL:
        a, b = 3 if arg >> 15 else 0, (arg >> 14) & 1
    elif op == MOV:
        a, b, c, d = arg >> 12, (arg >> 11) & 1, (arg >> 7) & 15, arg & 63
    elif op == SET:
        a = arg >> 13
        if a in (T_X, T_Y):
            b = arg & 0x1FFF
        else:
            b, c, d = arg & 63, (arg >> 9) & 15, ((arg >> 6) & 7) + 1
    elif op == CTL:
        a, b = arg >> 12, arg & 0xFFF
    return (op, t, se, sv, dl, a, b, c, d)
