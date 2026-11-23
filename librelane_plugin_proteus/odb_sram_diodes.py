# SPDX-FileCopyrightText: © 2026 Ken Pettit
# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
#
# proteus: odb_sram_diodes.py from Ken Pettit's https://github.com/kdp1965/ihp-um-janestreet-prism (main,
# 2026-10-15; Apache-2.0, the same license as this repository). The code is his with two changes: a pin behind
# one of proteus's pin buffers gets its diode on that buffer's input (long_wire_end), and a diode beside a
# standard cell takes the pin's side, because LibreLane picks a random side when no source is given. This
# comment is new:
# his said the SRAM LEF carries no antenna data, but the LEF at IHP-Open-PDK 89d42ee6d2fd (the same file as at
# his 2bbec755dc67) has ANTENNAGATEAREA on every input. His walkthrough gives the reason he kept the step: in his
# flow, OpenROAD's rule-based repair did not always protect the nets that drive the SRAM. Run by the
# Proteus.DiodesOnSramPins step in __init__.py, inside OpenROAD's Python.
#
# One antenna diode for every signal input pin of every hard macro whose master name starts with --macro-prefix
# (the IHP SRAMs). Placement is the LibreLane inserter's: the nearest row point to a macro pin, or beside a
# buffer. It is legalised by the composite step's detailed placement.
import click
import odb

from reader import click_odb   # LibreLane's odbpy directory is on the path for OdbpyStep scripts
from diodes import DiodeInserter


def long_wire_end(pin, net_prefix):
    """The terminal where the wire to `pin` ends: its buffer's input if odb_sram_buffers.py gave it one."""
    if not pin.getNet().getName().startswith(net_prefix):
        return pin
    buf = next(t.getInst() for t in pin.getNet().getITerms() if t.isOutputSignal())
    return next(t for t in buf.getITerms() if t.isInputSignal())


class MacroPinDiodes(DiodeInserter):
    def __init__(self, reader, macro_prefix, net_prefix, **kwargs):
        super().__init__(reader, **kwargs)
        self.macro_prefix = macro_prefix
        self.net_prefix = net_prefix

    def execute(self):
        self.count = 0
        for inst in list(self.block.getInsts()):
            master = inst.getMaster()
            if not master.isBlock() or not master.getName().startswith(self.macro_prefix):
                continue
            n = moved = 0
            for it in inst.getITerms():
                if not it.isInputSignal():
                    continue
                net = it.getNet()
                if net is None or net.isSpecial():
                    continue
                end = long_wire_end(it, self.net_prefix)
                self.insert_diode(end.getNet(), end, None)
                n += 1
                moved += end is not it
            self.count += n
            print(f"[INFO] {inst.getName()}: diodes for {n} input pins, {moved} of them at the pin's buffer")


@click.command()
@click.option("-c", "--diode-cell", required=True, help="diode cell name")
@click.option("-p", "--diode-pin", required=True, help="diode cell pin")
@click.option("--macro-prefix", required=True, help="master name prefix of the macros to protect")
@click.option("--net-prefix", required=True, help="name prefix of the nets between the pins and their buffers")
@click_odb
def sram_diodes(reader, diode_cell, diode_pin, macro_prefix, net_prefix):
    di = MacroPinDiodes(reader, macro_prefix, net_prefix, diode_cell=diode_cell, diode_pin=diode_pin,
                        side_strategy="pin", threshold_microns=0, port_protect_polarities=[], verbose=False)
    di.execute()
    print(f"[INFO] inserted {di.count} diodes for {macro_prefix}* input pins")


if __name__ == "__main__":
    sram_diodes()
