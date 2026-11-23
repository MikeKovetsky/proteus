"""The 64-bit table entry and the image layout, shared by the compiler and the simulator.

One state is one entry: word A in the first 512x32 SRAM macro, word B in the second, both at the same
address. The next address is `base | cA | cB << 1`, so a state's successors sit side by side in the table.
The one exception is a return (`nop` = 3): the next address is the machine's link register, which a call
loaded from `imm[8:0]` (`dst` = 7).

Word A (control)                         Word B (datapath)
  [8:0]   base   successor block           [15:0]  imm    T, or J[15:12] K[11:8] byte[7:0]
  [12:9]  cA     condition -> addr bit 0   [18:16] src    bus source
  [16:13] cB     condition -> addr bit 1   [21:19] dst    bus destination
  [18:17] p0     logical output            [22]    shift  shift S one place, `din` enters
  [21:19] p0op   pin operation             [25:23] crc    LFSR operation
  [23:22] p1     logical output            [27:26] nop    N: 0 keep, 1 dec, 2 inc; 3 is `ret`
  [26:24] p1op   pin operation             [30:28] kj     K and J: keep, dec or load each
  [28:27] gate   pin ops: 0 always,        [31]    tload  T = imm
                 1 if cA, 2 if not cA
  [29]    dsel   data bit: 0 S, 1 LFSR
  [31:30] din    serial input: 0, 1, A, B
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

DEPTH = 512          # entries: two 512x32 macros read side by side
ENTRY_BYTES = 8
MAX_MACHINES = 4
MACHINE_CFG_BYTES = 16
MAX_TCAM = 16

# Gate field (2 bits): 0..2 gate pin operations; 3 selects 1-clock indexed / TCAM dispatch (pin ops off).
GATE_ALWAYS, GATE_IF, GATE_UNLESS, GATE_DISPATCH = range(4)

# Indexed dispatch modes (when gate == GATE_DISPATCH, selected by ca[1:0]):
#   0: 4-way S 2-bit slice  {base[8:2], (S >> cb[2:0]) & 3}
#   1: 16-way S nibble      {base[8:4], (S >> 4) if cb[0] else (S & 15)}
#   2: 8-way TCAM class     {base[8:3], tcam_cls & 7}
#   3: 16-way (cb[0]==0) or 4-way (cb[0]==1) TCAM class
DISP_SLICE2, DISP_NIBBLE, DISP_TCAM8, DISP_TCAM16 = range(4)

# Conditions a state can branch on (4-bit select). All are flags of registered state.
CONDS = ["0", "a", "b", "c", "edge", "tz", "kz", "jz", "nz", "d", "crc", "rx0", "rx3", "tx1", "tx2", "eq"]
C_ZERO, C_A, C_B, C_C, C_EDGE, C_TZ, C_KZ, C_JZ, C_NZ, C_D, C_CRC, C_RX0, C_RX3, C_TX1, C_TX2, C_EQ = range(16)

# Pin operations (3 bits).
PIN_OPS = ["keep", "0", "1", "z", "d", "!d", "toggle", "od"]
P_KEEP, P_LOW, P_HIGH, P_RELEASE, P_DATA, P_NDATA, P_TOGGLE, P_OD = range(8)

# Bus sources and destinations (3 bits each). One byte moves per state.
SRCS = ["imm", "pop0", "pop3", "S", "R", "Nlo", "Nhi", "-"]
SRC_IMM, SRC_POP0, SRC_POP3, SRC_S, SRC_R, SRC_NLO, SRC_NHI = range(7)
DSTS = ["-", "S", "R", "N", "Nhi", "push1", "push2", "link"]
DST_NONE, DST_S, DST_R, DST_N, DST_NHI, DST_PUSH1, DST_PUSH2, DST_LINK = range(8)
N_KEEP, N_DEC, N_INC, N_RETURN = range(4)  # the N field; its fourth code is the return

# LFSR operations (3 bits).
CRC_OPS = ["keep", "=0", "=init", "+=d", "+=in", ">>=1"]
CRC_KEEP, CRC_ZERO, CRC_INIT, CRC_STEP_D, CRC_STEP_IN, CRC_SHIFT = range(6)

# Serial input select (2 bits).
DIN_0, DIN_1, DIN_A, DIN_B = range(4)

# K/J operation: code = 3 * k + j with 0 keep, 1 dec, 2 load. (load, load) does not exist.
KEEP, DEC, LOAD = range(3)

FIELDS = (
    "base", "ca", "cb", "p0", "p0op", "p1", "p1op", "gate", "dsel", "din",
    "imm", "src", "dst", "shift", "crc", "nop", "kj", "tload",
)


def pack(f: Dict[str, int]) -> Tuple[int, int]:
    """Fields -> (word A, word B)."""
    a = (
        (f["base"] & 0x1FF)
        | (f["ca"] & 15) << 9
        | (f["cb"] & 15) << 13
        | (f["p0"] & 3) << 17
        | (f["p0op"] & 7) << 19
        | (f["p1"] & 3) << 22
        | (f["p1op"] & 7) << 24
        | (f["gate"] & 3) << 27
        | (f["dsel"] & 1) << 29
        | (f["din"] & 3) << 30
    )
    b = (
        (f["imm"] & 0xFFFF)
        | (f["src"] & 7) << 16
        | (f["dst"] & 7) << 19
        | (f["shift"] & 1) << 22
        | (f["crc"] & 7) << 23
        | (f["nop"] & 3) << 26
        | (f["kj"] & 7) << 28
        | (f["tload"] & 1) << 31
    )
    return a, b


def unpack(a: int, b: int) -> Tuple[int, ...]:
    """(word A, word B) -> the fields, in the order of FIELDS."""
    return (
        a & 0x1FF, (a >> 9) & 15, (a >> 13) & 15, (a >> 17) & 3, (a >> 19) & 7, (a >> 22) & 3, (a >> 24) & 7,
        (a >> 27) & 3, (a >> 29) & 1, (a >> 30) & 3,
        b & 0xFFFF, (b >> 16) & 7, (b >> 19) & 7, (b >> 22) & 1, (b >> 23) & 7, (b >> 26) & 3, (b >> 28) & 7,
        (b >> 31) & 1,
    )


def blank() -> Dict[str, int]:
    return {name: 0 for name in FIELDS}


class MachineConfig:
    """What the loader writes into one machine's configuration flops (16 bytes in the image)."""

    def __init__(self) -> None:
        self.out: List[int] = [0, 0, 0, 0]  # logical output -> pin
        self.inp: List[int] = [0, 0, 0]     # inputs A, B, C -> pin
        self.msb = 0                        # shifter order: 0 LSB first, 1 MSB first
        self.poly = 0                       # LFSR polynomial, reflected
        self.init = 0                       # what `crc=init` loads
        self.match = 0                      # the `crc` condition is LFSR == match
        # v2 extensions packed into spare bits of head[0..3] (all 0 in every v1 image):
        self.in_b_half = 0                  # head[0] bit 6: sample input B from falling-edge (8.33 ns earlier) copy
        self.edge_b = 0                     # head[0] bit 7: `edge` watches input B as well as input A
        self.in_d_en = 0                    # head[2] bit 6: cond 12 selects 4th input D instead of rx3
        self.in_d = 0                       # head[3] bits 6:4: 4th input D -> pin

    def to_bytes(self) -> bytes:
        o, i = self.out, self.inp
        head = bytes([
            (o[0] & 7) | (o[1] & 7) << 3 | (self.in_b_half & 1) << 6 | (self.edge_b & 1) << 7,
            (o[2] & 7) | (o[3] & 7) << 3,
            (i[0] & 7) | (i[1] & 7) << 3 | (self.in_d_en & 1) << 6,
            (i[2] & 7) | (self.msb & 1) << 3 | (self.in_d & 7) << 4,
        ])
        return (head + self.poly.to_bytes(4, "little") + self.init.to_bytes(4, "little")
                + self.match.to_bytes(4, "little"))

    @classmethod
    def from_bytes(cls, raw: bytes) -> "MachineConfig":
        cfg = cls()
        cfg.out = [raw[0] & 7, (raw[0] >> 3) & 7, raw[1] & 7, (raw[1] >> 3) & 7]
        cfg.in_b_half = (raw[0] >> 6) & 1
        cfg.edge_b = (raw[0] >> 7) & 1
        cfg.inp = [raw[2] & 7, (raw[2] >> 3) & 7, raw[3] & 7]
        cfg.in_d_en = (raw[2] >> 6) & 1
        cfg.msb = (raw[3] >> 3) & 1
        cfg.in_d = (raw[3] >> 4) & 7
        cfg.poly = int.from_bytes(raw[4:8], "little")
        cfg.init = int.from_bytes(raw[8:12], "little")
        cfg.match = int.from_bytes(raw[12:16], "little")
        return cfg


def build_image(configs: List[MachineConfig], words: List[Tuple[int, int]],
                tcam: Optional[List[Tuple[int, int, int]]] = None) -> bytes:
    """Image:
    v1 (no TCAM): [machines][16 B/machine][entries, 2 B][8 B/entry]
    v2 (with TCAM): [machines | 0x80][16 B/machine][tcam_count][3 B/rule: val, mask, cls][entries, 2 B][8 B/entry]
    """
    has_tcam = bool(tcam)
    out = bytearray([len(configs) | (0x80 if has_tcam else 0)])
    for cfg in configs:
        out += cfg.to_bytes()
    if has_tcam:
        assert tcam is not None
        out.append(len(tcam))
        for val, mask, cls in tcam:
            out.extend((val & 0xFF, mask & 0xFF, cls & 0x0F))
    out += len(words).to_bytes(2, "little")
    for a, b in words:
        out += a.to_bytes(4, "little") + b.to_bytes(4, "little")
    return bytes(out)


def parse_image_v2(image: bytes) -> Tuple[List[MachineConfig], List[Tuple[int, int]], List[Tuple[int, int, int]]]:
    hdr = image[0]
    has_tcam = bool(hdr & 0x80)
    n = hdr & 0x7F
    pos = 1
    configs = []
    for _ in range(n):
        configs.append(MachineConfig.from_bytes(image[pos:pos + MACHINE_CFG_BYTES]))
        pos += MACHINE_CFG_BYTES
    tcam: List[Tuple[int, int, int]] = []
    if has_tcam:
        tcnt = image[pos]
        pos += 1
        for _ in range(tcnt):
            tcam.append((image[pos], image[pos + 1], image[pos + 2] & 0x0F))
            pos += 3
    count = int.from_bytes(image[pos:pos + 2], "little")
    pos += 2
    words = []
    for _ in range(count):
        words.append((int.from_bytes(image[pos:pos + 4], "little"), int.from_bytes(image[pos + 4:pos + 8], "little")))
        pos += ENTRY_BYTES
    return configs, words, tcam


def parse_image(image: bytes) -> Tuple[List[MachineConfig], List[Tuple[int, int]]]:
    configs, words, _tcam = parse_image_v2(image)
    return configs, words
