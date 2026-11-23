# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
#
# One buffer beside every live signal pin of both table SRAMs. Run by the Proteus.BuffersOnSramPins step in
# __init__.py, inside OpenROAD's Python, between clock tree synthesis and the post-CTS timing repair.
#
# The SRAM's limits are far tighter than a standard cell's: 0.5952 ns of slew on an input at the slow corner
# (2.5 ns on a cell) and 0.064 pF of load on an output (0.3 pF and up on a cell). With a buffer at the pin,
# the SRAM sees only a short wire, and the long wire has to meet only the standard cells' limits.
#
# The step makes one new net per pin, between the pin and its buffer, named <net prefix><macro>_<pin>. The
# long wire keeps the old net. The names have no brackets or dots, so RSZ_DONT_TOUCH_RX (src/config.json) can
# hold the new nets still and no hold buffer can land between a buffer and its pin.
import re
import sys

import click
import odb

from reader import click_odb   # LibreLane's odbpy directory is on the path for OdbpyStep scripts

LIVE_INPUT = re.compile(r"A_(MEN|WEN|REN|ADDR\[\d+\]|DIN\[\d+\]|BM\[\d+\])")
LIVE_OUTPUT = re.compile(r"A_DOUT\[\d+\]")
MACROS, INPUTS, OUTPUTS = 2, 76, 32   # src/proteus_table.v: A_CLK is CTS's, and A_DLY and the BIST pins are tied
BUFFER_PREFIX = "sram_pin_buf_"


def fail(message):
    print(f"[ERROR] {message}", file=sys.stderr)
    sys.exit(1)


def live_pins(block, macro_prefix):
    """Every pin to buffer, as (macro, pin, is_input, x, y). It changes nothing, and stops on any surprise."""
    macros = [i for i in block.getInsts() if i.getMaster().isBlock() and i.getMaster().getName().startswith(macro_prefix)]
    if len(macros) != MACROS:
        fail(f"found {len(macros)} {macro_prefix}* macros, expected {MACROS}")
    pins = []
    for macro in macros:
        count = {True: 0, False: 0}
        for it in macro.getITerms():
            name = it.getMTerm().getConstName()
            is_input = LIVE_INPUT.fullmatch(name) is not None
            if not is_input and LIVE_OUTPUT.fullmatch(name) is None:
                continue
            where = f"{macro.getName()}/{name}"
            if it.getNet() is None:
                fail(f"{where} is not connected")
            found, x, y = it.getAvgXY()
            if not found:
                fail(f"{where} has no location")
            count[is_input] += 1
            pins.append((macro, it, is_input, x, y))
        if (count[True], count[False]) != (INPUTS, OUTPUTS):
            fail(f"{macro.getName()} has {count[True]} live inputs and {count[False]} outputs, expected {INPUTS} and {OUTPUTS}")
    return pins


def nearest_spot(rows, x, y, width):
    """The legal-row position nearest (x, y) for a cell `width` wide, centred on x: (x, y, orientation)."""
    best = None
    for row in rows:
        box = row.getBBox()
        if box.xMax() - box.xMin() < width:
            continue
        left = min(max(x - width // 2, box.xMin()), box.xMax() - width)
        distance = abs(left + width // 2 - x) + max(box.yMin() - y, 0, y - box.yMax())
        if best is None or distance < best[0]:
            best = (distance, left, box.yMin(), row.getOrient())
    if best is None:
        fail(f"no row is {width} database units wide")
    return best[1:]


def add_buffers(block, master, macro_prefix, net_prefix):
    """Puts a `master` buffer between each live pin and its net. Returns how many it added."""
    pins = live_pins(block, macro_prefix)
    rows = list(block.getRows())
    for macro, pin, is_input, x, y in pins:
        short = macro.getName().split(".")[-1] + "_" + pin.getMTerm().getConstName()
        short = re.sub(r"\W+", "_", short).strip("_")
        long_net = pin.getNet()
        pin_net = odb.dbNet.create(block, net_prefix + short)
        buf = odb.dbInst.create(block, master, BUFFER_PREFIX + short)
        if pin_net is None or buf is None:
            fail(f"{short} already has a buffer")
        buf_in = [t for t in buf.getITerms() if t.isInputSignal()]
        buf_out = [t for t in buf.getITerms() if t.isOutputSignal()]
        if len(buf_in) != 1 or len(buf_out) != 1:
            fail(f"{master.getName()} is not a buffer")

        pin.disconnect()
        pin.connect(pin_net)
        if is_input:
            buf_in[0].connect(long_net)
            buf_out[0].connect(pin_net)
        else:
            buf_in[0].connect(pin_net)
            buf_out[0].connect(long_net)

        left, bottom, orient = nearest_spot(rows, x, y, master.getWidth())
        buf.setOrient(orient)
        buf.setLocation(left, bottom)
        buf.setPlacementStatus("PLACED")   # legalised by the composite step's detailed placement
    return len(pins)


@click.command()
@click.option("--buffer-cell", required=True, help="buffer cell name")
@click.option("--macro-prefix", required=True, help="master name prefix of the SRAMs")
@click.option("--net-prefix", required=True, help="name prefix of the nets between the buffers and the pins")
@click_odb
def sram_buffers(reader, buffer_cell, macro_prefix, net_prefix):
    master = reader.db.findMaster(buffer_cell)
    if master is None:
        fail(f"no cell {buffer_cell}")
    n = add_buffers(reader.block, master, macro_prefix, net_prefix)
    print(f"[INFO] inserted {n} {buffer_cell} beside the SRAM pins ({INPUTS} inputs and {OUTPUTS} outputs on each macro)")


if __name__ == "__main__":
    sram_buffers()
