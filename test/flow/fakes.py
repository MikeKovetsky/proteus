# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""
Just enough of OpenROAD's odb, click and LibreLane's Python to run librelane_plugin_proteus without them.

The odb calls keep the behaviour the scripts rely on (OpenROAD dcf36133a369, src/odb/src/db): create() returns
None for a name that exists, connect() moves a terminal, and power pins are neither signal inputs nor outputs.
"""
import importlib.util
import os
import sys
import types
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLUGIN = os.path.join(ROOT, "librelane_plugin_proteus")
SRAM = "RM_IHPSG13_1P_512x32_c2_bm_bist"


class Master:
    def __init__(self, name, ports, width=2400, block=False):
        self.name, self.ports, self.width, self.block = name, ports, width, block

    def getName(self):
        return self.name

    def getWidth(self):
        return self.width

    def isBlock(self):
        return self.block


class MTerm:
    def __init__(self, name):
        self.name = name

    def getConstName(self):
        return self.name


class ITerm:
    def __init__(self, inst, port, direction, at=(0, 0)):
        self.inst, self.port, self.direction, self.at, self.net = inst, port, direction, at, None

    def getInst(self):
        return self.inst

    def getMTerm(self):
        return MTerm(self.port)

    def getNet(self):
        return self.net

    def getAvgXY(self):
        return (True,) + self.at if self.at else (False, 0, 0)

    def isInputSignal(self):
        return self.direction == "input"

    def isOutputSignal(self):
        return self.direction == "output"

    def disconnect(self):
        if self.net is not None:
            self.net.terms.remove(self)
            self.net = None

    def connect(self, net):
        self.disconnect()
        self.net = net
        net.terms.append(self)


class Inst:
    def __init__(self, master, name, positions=None):
        self.master, self.name = master, name
        self.location = self.orient = self.status = None
        positions = positions or {}
        self.terms = [ITerm(self, p, d, positions.get(p, (0, 0))) for p, d in master.ports]

    def getName(self):
        return self.name

    def getMaster(self):
        return self.master

    def getITerms(self):
        return self.terms

    def pin(self, port):
        return next(t for t in self.terms if t.port == port)

    def setOrient(self, orient):
        self.orient = orient

    def setLocation(self, x, y):
        self.location = (x, y)

    def setPlacementStatus(self, status):
        self.status = status


class Net:
    def __init__(self, name):
        self.name, self.terms = name, []

    def getName(self):
        return self.name

    def isSpecial(self):
        return False

    def getITerms(self):
        return list(self.terms)


class Rect:
    def __init__(self, *box):
        self.box = box

    def xMin(self):
        return self.box[0]

    def yMin(self):
        return self.box[1]

    def xMax(self):
        return self.box[2]

    def yMax(self):
        return self.box[3]


class Row:
    def __init__(self, box, orient="R0"):
        self.box, self.orient = Rect(*box), orient

    def getBBox(self):
        return self.box

    def getOrient(self):
        return self.orient


class Block:
    def __init__(self):
        self.insts, self.nets, self.rows = {}, {}, []

    def getInsts(self):
        return list(self.insts.values())

    def getRows(self):
        return self.rows


class DbInst:
    @staticmethod
    def create(block, master, name):
        if name in block.insts:
            return None
        block.insts[name] = Inst(master, name)
        return block.insts[name]


class DbNet:
    @staticmethod
    def create(block, name):
        if name in block.nets:
            return None
        block.nets[name] = Net(name)
        return block.nets[name]


class DiodeInserter:
    """Stands in for LibreLane's diodes.DiodeInserter: it records what it was asked to protect."""

    def __init__(self, reader, **kwargs):
        self.reader, self.block, self.options = reader, reader.block, kwargs
        self.protected = []

    def insert_diode(self, net, iterm, src_pos):
        self.protected.append((net, iterm))


def load_script(name):
    """One of the plugin's OpenROAD scripts as a module, and the options its command takes."""
    options = []

    def option(*flags, **_):
        options.extend(flags)
        return lambda function: function

    stubs = {
        "click": types.SimpleNamespace(command=lambda: (lambda function: function), option=option),
        "odb": types.SimpleNamespace(dbInst=DbInst, dbNet=DbNet),
        "reader": types.SimpleNamespace(click_odb=lambda function: function),
        "diodes": types.SimpleNamespace(DiodeInserter=DiodeInserter),
    }
    spec = importlib.util.spec_from_file_location(name, os.path.join(PLUGIN, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module, options


def load_plugin():
    """The plugin package and the steps it registers, by ID."""
    registered = {}

    class Factory:
        @staticmethod
        def register():
            def add(step):
                registered[step.id] = step
                return step
            return add

    class Step:
        factory = Factory

        def __init__(self, config=None):
            self.config = config or {}

    class OdbpyStep(Step):
        config_vars = []

        def get_command(self):
            return ["openroad", "-python", self.get_script_path()]

    class CompositeStep(Step):
        pass

    class DetailedPlacement(Step):
        id = "OpenROAD.DetailedPlacement"

    def variable(name, kind, description, default=None, **_):
        return types.SimpleNamespace(name=name, default=default)

    def module(name, **attributes):
        m = types.ModuleType(name)
        m.__dict__.update(attributes)
        return m

    netgen = module("librelane.steps.netgen")
    steps = module("librelane.steps", Step=Step, netgen=netgen)
    root = module("librelane", steps=steps)
    stubs = {
        "librelane": root,
        "librelane.config": module("librelane.config", Variable=variable),
        "librelane.steps": steps,
        "librelane.steps.netgen": netgen,
        "librelane.steps.odb": module("librelane.steps.odb", OdbpyStep=OdbpyStep),
        "librelane.steps.openroad": module("librelane.steps.openroad", DetailedPlacement=DetailedPlacement),
        "librelane.steps.step": module("librelane.steps.step", CompositeStep=CompositeStep),
    }
    spec = importlib.util.spec_from_file_location("plugin_under_test", os.path.join(PLUGIN, "__init__.py"))
    plugin = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, stubs):
        spec.loader.exec_module(plugin)
    return plugin, registered


def sram_ports():
    """The macro's pins, as in src/RM_IHPSG13_1P_512x32_c2_bm_bist_bb.v, and its three supplies."""
    ports = [(p, "input") for p in ("A_CLK", "A_MEN", "A_WEN", "A_REN", "A_DLY",
                                    "A_BIST_CLK", "A_BIST_EN", "A_BIST_MEN", "A_BIST_WEN", "A_BIST_REN")]
    for bus, width, direction in (("A_ADDR", 9, "input"), ("A_DIN", 32, "input"), ("A_BM", 32, "input"),
                                  ("A_DOUT", 32, "output"), ("A_BIST_ADDR", 9, "input"),
                                  ("A_BIST_DIN", 32, "input"), ("A_BIST_BM", 32, "input")):
        ports += [("%s[%d]" % (bus, i), direction) for i in range(width)]
    return ports + [(p, "power") for p in ("VDD!", "VDDARRAY!", "VSS!")]


def table_design():
    """
    Both SRAMs wired as the mapped design wires them: the address and controls are shared, each din and bm bit
    position of a byte is one net, the clock and ties have drivers of their own, and each DOUT has a load.
    Positions are database units (nm), from src/config.json's placement.
    """
    block = Block()
    sram = Master(SRAM, sram_ports(), width=416640, block=True)
    driver = Master("sg13cmos5l_and2_1", [("X", "output"), ("VDD", "power")])
    load = Master("sg13cmos5l_and2_1", [("A", "input"), ("VDD", "power")])

    def net_for(name):
        if name not in block.nets:
            net = DbNet.create(block, name)
            DbInst.create(block, driver, "drive_" + name).pin("X").connect(net)
        return block.nets[name]

    for tag, pin_y in (("a", 195120), ("b", 514080)):
        name = "core.table_ram.ram_" + tag
        positions = {p: (273120 + 1500 + 1950 * i, pin_y) for i, (p, _) in enumerate(sram.ports)}
        macro = block.insts[name] = Inst(sram, name, positions)
        for pin in macro.terms:
            base, _, index = pin.port.rstrip("]").partition("[")
            if pin.direction == "power":
                continue
            if base == "A_DOUT":
                net = DbNet.create(block, "core.q_%s[%s]" % (tag, index))
                pin.connect(net)
                DbInst.create(block, load, "use_%s_%s" % (tag, index)).pin("A").connect(net)
            elif "BIST" in base or base == "A_DLY":
                pin.connect(net_for("tie_%s_%s" % (tag, pin.port)))
            elif base in ("A_DIN", "A_BM"):
                pin.connect(net_for("core.%s[%d]" % (base, int(index) % 8)))
            else:
                pin.connect(net_for("core.%s" % pin.port))

    # Rows under, between and over the two macros, and beside them, as CutRows leaves them.
    y, flip = 3780, False
    while y + 3780 <= 706860:
        orient = "MX" if flip else "R0"
        if 195600 <= y and y + 3780 <= 513600:
            block.rows.append(Row((3360, y, 1696320, y + 3780), orient))
        else:
            block.rows.append(Row((3360, y, 270720, y + 3780), orient))
            block.rows.append(Row((692160, y, 1696320, y + 3780), orient))
        y, flip = y + 3780, not flip
    return block
