# SPDX-FileCopyrightText: © 2026 Ken Pettit
# SPDX-License-Identifier: Apache-2.0
#
# protoemu: odb_sram_diodes.py from Ken Pettit's https://github.com/kdp1965/ihp-um-janestreet-prism (main,
# 2026-10-15; Apache-2.0, the same license as this repository). The code is his, unchanged. This comment is new:
# his said the SRAM LEF carries no antenna data, but the LEF at IHP-Open-PDK 89d42ee6d2fd (the same file as at
# his 2bbec755dc67) has ANTENNAGATEAREA on every input. His walkthrough gives the reason he kept the step: in his
# flow, OpenROAD's rule-based repair did not always protect the nets that drive the SRAM. Run by the
# Protoemu.DiodesOnSramPins step in __init__.py, inside OpenROAD's Python.
#
# One antenna diode on every signal input pin of every hard macro whose master name starts with --macro-prefix
# (the IHP SRAMs). Placement is the LibreLane inserter's macro rule (the nearest row point to the pin) and is
# legalised by the composite step's detailed placement.
import click
import odb

from reader import click_odb   # LibreLane's odbpy directory is on the path for OdbpyStep scripts
from diodes import DiodeInserter


class MacroPinDiodes(DiodeInserter):
    def __init__(self, reader, macro_prefix, **kwargs):
        super().__init__(reader, **kwargs)
        self.macro_prefix = macro_prefix

    def execute(self):
        self.count = 0
        for inst in list(self.block.getInsts()):
            master = inst.getMaster()
            if not master.isBlock() or not master.getName().startswith(self.macro_prefix):
                continue
            n = 0
            for it in inst.getITerms():
                if not it.isInputSignal():
                    continue
                net = it.getNet()
                if net is None or net.isSpecial():
                    continue
                self.insert_diode(net, it, None)
                n += 1
            self.count += n
            print(f"[INFO] {inst.getName()}: diodes on {n} input pins")


@click.command()
@click.option("-c", "--diode-cell", required=True, help="diode cell name")
@click.option("-p", "--diode-pin", required=True, help="diode cell pin")
@click.option("--macro-prefix", default="RM_IHPSG13", help="master name prefix of the macros to protect")
@click_odb
def sram_diodes(reader, diode_cell, diode_pin, macro_prefix):
    di = MacroPinDiodes(reader, macro_prefix, diode_cell=diode_cell, diode_pin=diode_pin,
                        side_strategy="source", threshold_microns=0, port_protect_polarities=[], verbose=False)
    di.execute()
    print(f"[INFO] inserted {di.count} diodes on {macro_prefix}* input pins")


if __name__ == "__main__":
    sram_diodes()
