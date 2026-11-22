# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-FileCopyrightText: © 2026 Ken Pettit (ihp-um-janestreet-prism, Apache-2.0)
# SPDX-License-Identifier: Apache-2.0
"""
LibreLane plugin for protoemu: power the two IHP SRAM macros on the Tiny Tapeout CMOS5L tile, buffer their
signal pins, and protect their inputs from the antenna effect.

Adapted from Ken Pettit's librelane_plugin_prism_pdn.py in https://github.com/kdp1965/ihp-um-janestreet-prism
(main, 2026-10-15), which hardens two of these macros at 8x4 and passes precheck. Only the three pieces this
design needs are kept: the stripe step, the diodes on the SRAM inputs, and the Netgen report fix. The buffers
on the SRAM pins are protoemu's own.

How it gets loaded: LibreLane imports every module on the Python path whose name starts with
``librelane_plugin_`` (librelane/plugins.py), and ``python3 -m librelane`` puts the current directory first on
that path. Tiny Tapeout's GDS action runs ``tt_tool.py --harden``, which runs LibreLane from the repository
root, on the host and again inside LibreLane's container (same working directory), so this package is found
both times without installing anything. src/config.json then adds the three steps to the Classic flow:

    "meta": {"flow": "Classic", "substituting_steps": {
        "+OpenROAD.GeneratePDN": "Protoemu.ExtendPowerStripes",
        "+OpenROAD.CTS": "Protoemu.BuffersOnSramPins",
        "+OpenROAD.ResizerTimingPostCTS": "Protoemu.DiodesOnSramPins"}}

Nothing here may import ``odb``: that module exists only inside OpenROAD, where the odb_*.py scripts run.
"""
import json as _json
import os
import re as _re
import types as _types
from decimal import Decimal

from librelane.config import Variable
from librelane.steps import Step
from librelane.steps.odb import OdbpyStep
from librelane.steps.openroad import DetailedPlacement
from librelane.steps.step import CompositeStep

import librelane.steps.netgen as _netgen

HERE = os.path.dirname(os.path.abspath(__file__))
MACRO_PREFIX = "RM_IHPSG13"  # the IHP SRAMs' master names
PIN_NET_PREFIX = "sram_pin_net_"  # the nets between the pins and their buffers; RSZ_DONT_TOUCH_RX matches it


@Step.factory.register()
class ExtendPowerStripes(OdbpyStep):
    """
    Runs full-height Metal4 VPWR/VGND stripes over the IHP SRAMs' own supply columns (odb_stripes.py).

    The tile's grid is vertical Metal4 stripes only, and pdngen cuts them around a macro, so the SRAM's
    Metal4 VDD!, VDDARRAY! and VSS! columns get no power, and the stubs pdngen leaves above and below it are
    power pins that do not span the tile, which Tiny Tapeout's pin check rejects. This step replaces every
    stripe that crosses an SRAM with stripes drawn exactly on the SRAM's columns of the same polarity, from
    the bottom to the top of the core, adds the rail vias where they cross standard-cell rows, and leaves one
    full-height box per stripe.
    """

    id = "Protoemu.ExtendPowerStripes"
    name = "Extend Power Stripes Over the SRAM Macros"

    config_vars = [
        Variable("EXTEND_STRIPES_LAYER", str, "The tile's vertical single-layer PDN layer.", default="Metal4"),
        Variable(
            "EXTEND_STRIPES_SRAM_LAYER",
            str,
            "Layer of the IHP SRAM power columns (Metal4 on CMOS5L, the same as the stripes).",
            default="Metal4",
        ),
        Variable(
            "EXTEND_STRIPES_CLEARANCE",
            Decimal,
            "Spacing kept between a drawn stripe and the other net's macro rails or tile pins on the stripe layer.",
            units="µm",
            default=Decimal("0.24"),
        ),
        Variable(
            "EXTEND_STRIPES_STACK_PITCH",
            Decimal,
            "Spacing of the via stacks along an SRAM power column (only used when the column layer differs).",
            units="µm",
            default=Decimal("10"),
        ),
        Variable(
            "EXTEND_STRIPES_PIN_FACE_MARGIN",
            Decimal,
            "No rail via stack within this distance of a macro edge that carries pins; 0 leaves pdngen's alone.",
            units="µm",
            default=Decimal("0"),
        ),
        Variable(
            "EXTEND_STRIPES_SRAM_ALL_COLUMNS",
            bool,
            "Put a stripe on every legal supply column of an IHP SRAM.",
            default=False,
        ),
        Variable(
            "EXTEND_STRIPES_SRAM_ARRAY_EVERY_OTHER",
            bool,
            "In each bit-cell array of an IHP SRAM, add a VPWR/VGND pair on every other pair position.",
            default=True,
        ),
    ]

    def get_script_path(self):
        return os.path.join(HERE, "odb_stripes.py")

    def get_command(self):
        cmd = super().get_command() + [
            "--layer",
            self.config["EXTEND_STRIPES_LAYER"],
            "--sram-layer",
            self.config["EXTEND_STRIPES_SRAM_LAYER"],
            "--clearance",
            str(self.config["EXTEND_STRIPES_CLEARANCE"]),
            "--stack-pitch",
            str(self.config["EXTEND_STRIPES_STACK_PITCH"]),
            "--pin-face-margin",
            str(self.config["EXTEND_STRIPES_PIN_FACE_MARGIN"]),
        ]
        cmd += ["--sram-all-columns" if self.config["EXTEND_STRIPES_SRAM_ALL_COLUMNS"] else "--sram-grid-columns"]
        if self.config["EXTEND_STRIPES_SRAM_ARRAY_EVERY_OTHER"]:
            cmd += ["--sram-array-every-other"]
        return cmd


@Step.factory.register()
class SramPinBufferPlacement(OdbpyStep):
    """One SRAM_PIN_BUFFER_CELL beside every live signal pin of the IHP SRAMs (odb_sram_buffers.py)."""

    id = "Protoemu.SramPinBufferPlacement"
    name = "Buffers on SRAM Signal Pins"

    config_vars = [
        Variable(
            "SRAM_PIN_BUFFER_CELL",
            str,
            "The buffer put beside each live signal pin of the IHP SRAMs.",
            default="sg13cmos5l_buf_2",
        ),
    ]

    def get_script_path(self):
        return os.path.join(HERE, "odb_sram_buffers.py")

    def get_command(self):
        return super().get_command() + [
            "--buffer-cell",
            self.config["SRAM_PIN_BUFFER_CELL"],
            "--macro-prefix",
            MACRO_PREFIX,
            "--net-prefix",
            PIN_NET_PREFIX,
        ]


@Step.factory.register()
class BuffersOnSramPins(CompositeStep):
    """
    Puts a buffer beside each of the 108 live signal pins of each SRAM, then legalises them with detailed
    placement.

    OpenROAD's design repair did not keep these pins inside the SRAM's limits. Its first pass runs before the
    hold buffers exist, and on this PDK the pass after global routing plans its buffers with zero wire
    resistance and capacitance (src/config.json, //sram_buffers). A buffer at the pin needs no estimate.

    It runs after clock tree synthesis and before OpenROAD.ResizerTimingPostCTS, because sharper edges raise
    the SRAM's hold requirement (by up to 0.31 ns at the fast corner) and that step is the one that repairs
    hold. Its hold buffers land upstream of these buffers: RSZ_DONT_TOUCH_RX freezes the short nets. Only the
    nets are frozen. A frozen buffer instance would make OpenROAD's hold repair drop the buffer's input from
    the pins it inserts in front of, and stop the flow with RSZ-3009 when that leaves none.
    """

    id = "Protoemu.BuffersOnSramPins"
    name = "Buffers on SRAM Pins"
    Steps = [SramPinBufferPlacement, DetailedPlacement]


@Step.factory.register()
class SramPinDiodePlacement(OdbpyStep):
    """One DIODE_CELL per SRAM input, where its long wire ends (odb_sram_diodes.py), from Pettit's plugin."""

    id = "Protoemu.SramPinDiodePlacement"
    name = "Diodes on SRAM Input Pins"

    def get_script_path(self):
        return os.path.join(HERE, "odb_sram_diodes.py")

    def get_command(self):
        cell, pin = self.config["DIODE_CELL"].split("/")
        return super().get_command() + [
            "--diode-cell",
            cell,
            "--diode-pin",
            pin,
            "--macro-prefix",
            MACRO_PREFIX,
            "--net-prefix",
            PIN_NET_PREFIX,
        ]


@Step.factory.register()
class DiodesOnSramPins(CompositeStep):
    """
    Puts an antenna diode for every input pin of the IHP SRAMs, then legalises them with detailed placement.

    The flow's rule-based repair (OpenROAD.RepairAntennas) should cover these nets, since the SRAM LEF carries
    antenna data, but Pettit found it did not always do so in his flow and added this step as cheap insurance:
    about 312 diode cells for the two macros. It runs before global routing (after
    OpenROAD.ResizerTimingPostCTS), so that one routing pass includes the diodes, and the rule-based repair
    still runs afterwards for every other net. From Pettit's plugin, with one change: for a pin that has a
    buffer from Protoemu.BuffersOnSramPins, the diode goes on the buffer's input, where the long wire now ends
    (a buf_2 gate alone allows about 210 um of minimum-width wire). Running after the hold repair matters for
    the same reason: the diode ends up on the last, long piece of the net.
    """

    id = "Protoemu.DiodesOnSramPins"
    name = "Diodes on SRAM Pins"
    Steps = [SramPinDiodePlacement, DetailedPlacement]


# Netgen writes the IHP SRAM's power pin names (VDD!, VSS!, VDDARRAY!) into its LVS JSON report with a stray
# backslash ("\VDD!"), which is not a valid JSON escape, and librelane.steps.netgen.LVS then fails in
# json.loads before the LVS checker runs. Give that module a json namespace whose loads() doubles such
# backslashes first. It only acts on a report that would not parse; the metrics only count entries.
# From Pettit's plugin, unchanged in substance.
_BAD_ESCAPE = _re.compile(r'\\(\\|[^"\\/bfnrtu])')


def _repair_escapes(s):
    return _BAD_ESCAPE.sub(lambda m: "\\\\" if m.group(1) == "\\" else "\\\\" + m.group(1), s)


def _loads_repairing_escapes(s, *args, **kwargs):
    try:
        return _json.loads(s, *args, **kwargs)
    except _json.JSONDecodeError:
        return _json.loads(_repair_escapes(s), *args, **kwargs)


_netgen.json = _types.SimpleNamespace(
    loads=_loads_repairing_escapes,
    load=_json.load,
    dumps=_json.dumps,
    dump=_json.dump,
    JSONDecodeError=_json.JSONDecodeError,
)
