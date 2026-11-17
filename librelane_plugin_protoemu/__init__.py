# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-FileCopyrightText: © 2026 Ken Pettit (ihp-um-janestreet-prism, Apache-2.0)
# SPDX-License-Identifier: Apache-2.0
"""
LibreLane plugin for protoemu: power the two IHP SRAM macros on the Tiny Tapeout CMOS5L tile.

Adapted from Ken Pettit's librelane_plugin_prism_pdn.py in https://github.com/kdp1965/ihp-um-janestreet-prism
(main, 2026-10-15), which hardens two of these macros at 8x4 and passes precheck. Only the two pieces this
design needs are kept: the stripe step and the Netgen report fix.

How it gets loaded: LibreLane imports every module on the Python path whose name starts with
``librelane_plugin_`` (librelane/plugins.py), and ``python3 -m librelane`` puts the current directory first on
that path. Tiny Tapeout's GDS action runs ``tt_tool.py --harden``, which runs LibreLane from the repository
root, on the host and again inside LibreLane's container (same working directory), so this package is found
both times without installing anything. src/config.json then adds the step after the PDN generator:

    "meta": {"flow": "Classic", "substituting_steps": {"+OpenROAD.GeneratePDN": "Protoemu.ExtendPowerStripes"}}

Nothing here may import ``odb``: that module exists only inside OpenROAD, where odb_stripes.py runs.
"""
import json as _json
import os
import re as _re
import types as _types
from decimal import Decimal

from librelane.config import Variable
from librelane.steps import Step
from librelane.steps.odb import OdbpyStep

import librelane.steps.netgen as _netgen

HERE = os.path.dirname(os.path.abspath(__file__))


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
