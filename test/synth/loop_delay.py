"""Estimate the decision loop's delay on the IHP-mapped netlist: SRAM data out -> logic -> SRAM address.

    python3 loop_delay.py build/ihp_netlist.json build/<stdcell>.lib build/<sram>.lib

This is a small static timing estimate, not a sign-off tool. It reads the netlist Yosys mapped to IHP
SG13CMOS5L cells (ihp.ys) and the slow-corner liberty files (1.08 V, 125 C), and propagates arrival times
and slews through every combinational cell with the liberty's delay tables (the worse of rise and fall on
every arc, so it is pessimistic about unateness). Each net's load is its sinks' pin capacitance plus the
liberty's "5k" wire-load model, the size of this design. Yosys maps with the smallest cells and no
buffers; a net with more fanout than the liberty's limit of 8 is given a tree of buffers, as the layout
flow's repair step would build. It knows nothing about placement, real wires, the clock tree or clock
skew; LibreLane's OpenSTA after routing is the real answer.

It reports two paths: the worst one that starts at the SRAM outputs and ends at the SRAM address (the
one-clock decision loop the model relies on), and the worst path in the whole chip.
"""
from __future__ import annotations

import bisect
import json
import math
import re
import sys
from typing import Dict, List, Optional, Tuple

CLOCK_NS = 16.6          # config.json's CLOCK_PERIOD
WIRE_LOAD = "5k"
SRAM = "RM_IHPSG13_1P_512x32_c2_bm_bist"


# ---- liberty -----------------------------------------------------------------------------------------------

class Group:
    def __init__(self, kind: str, args: List[str]) -> None:
        self.kind, self.args = kind, args
        self.attrs: Dict[str, str] = {}
        self.complex: Dict[str, List[List[str]]] = {}
        self.groups: List["Group"] = []

    def find(self, kind: str) -> List["Group"]:
        return [g for g in self.groups if g.kind == kind]


def parse_liberty(path: str) -> Group:
    text = open(path).read()
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S).replace("\\\n", " ").replace("\\\r\n", " ")
    tokens = re.findall(r'"[^"]*"|[{}();:,]|[^\s{}();:,"]+', text)
    pos = 0

    def unq(tok: str) -> str:
        return tok[1:-1] if tok.startswith('"') else tok

    def body(group: Group) -> None:
        nonlocal pos
        while pos < len(tokens):
            tok = tokens[pos]
            if tok == "}":
                pos += 1
                return
            name = tok
            pos += 1
            if tokens[pos] == ":":
                group.attrs[name] = unq(tokens[pos + 1])
                pos += 2
                if pos < len(tokens) and tokens[pos] == ";":
                    pos += 1
            elif tokens[pos] == "(":
                pos += 1
                args = []
                while tokens[pos] != ")":
                    if tokens[pos] != ",":
                        args.append(unq(tokens[pos]))
                    pos += 1
                pos += 1
                if pos < len(tokens) and tokens[pos] == "{":
                    pos += 1
                    child = Group(name, args)
                    body(child)
                    group.groups.append(child)
                else:
                    group.complex.setdefault(name, []).append(args)
                    if pos < len(tokens) and tokens[pos] == ";":
                        pos += 1
            else:
                pos += 1  # a stray token: skip it

    root = Group("root", [])
    body(root)
    return root.groups[0]


class Table:
    """A liberty lookup table. `at` takes its axes by name, as the table's template orders them."""

    def __init__(self, g: Group, templates: Dict[str, List[str]]) -> None:
        self.vars = templates.get(g.args[0] if g.args else "", [])
        self.i1 = [float(x) for x in g.complex["index_1"][0][0].split(",")]
        i2 = g.complex.get("index_2")
        self.i2 = [float(x) for x in i2[0][0].split(",")] if i2 else None
        rows = g.complex["values"][0]
        self.v = [[float(x) for x in row.split(",")] for row in rows]

    @staticmethod
    def _bracket(axis: List[float], x: float) -> Tuple[int, float]:
        if len(axis) == 1:
            return 0, 0.0
        j = min(max(bisect.bisect_right(axis, x) - 1, 0), len(axis) - 2)
        return j, (x - axis[j]) / (axis[j + 1] - axis[j])  # extrapolates linearly past either end

    def at(self, **axes: float) -> float:
        return self(axes[self.vars[0]], axes[self.vars[1]] if len(self.vars) > 1 else 0.0)

    def __call__(self, x1: float, x2: float = 0.0) -> float:
        a, fa = self._bracket(self.i1, x1)
        if self.i2 is None:
            row = self.v[0] if len(self.v) == 1 else [r[0] for r in self.v]
            return row[a] + fa * (row[a + 1] - row[a]) if len(row) > 1 else row[0]
        b, fb = self._bracket(self.i2, x2)
        v = self.v
        top = v[a][b] + fb * (v[a][b + 1] - v[a][b])
        bot = v[a + 1][b] + fb * (v[a + 1][b + 1] - v[a + 1][b])
        return top + fa * (bot - top)


class Arc:
    def __init__(self, g: Group, templates: Dict[str, List[str]]) -> None:
        self.related = g.attrs.get("related_pin", "")
        self.kind = g.attrs.get("timing_type", "combinational")
        tables = {t.kind: Table(t, templates) for t in g.groups}
        self.delay = [tables[k] for k in ("cell_rise", "cell_fall") if k in tables]
        self.slew = [tables[k] for k in ("rise_transition", "fall_transition") if k in tables]
        self.constraint = [tables[k] for k in ("rise_constraint", "fall_constraint") if k in tables]


class Cell:
    def __init__(self, g: Group, templates: Dict[str, List[str]]) -> None:
        self.name = g.args[0]
        self.cap: Dict[str, float] = {}
        self.direction: Dict[str, str] = {}
        self.arcs: Dict[str, List[Arc]] = {}  # output pin -> arcs; input pin -> constraint arcs
        for p in g.find("pin") + g.find("bus"):
            names = [p.args[0]] + [q.args[0] for q in p.find("pin")]
            for name in names:
                self.direction[name] = p.attrs.get("direction", "")
                self.cap[name] = float(p.attrs.get("capacitance", 0) or 0)
                self.arcs[name] = [Arc(t, templates) for t in p.find("timing")]


def cells_of(lib: Group) -> Dict[str, Cell]:
    templates = {}
    for t in lib.find("lu_table_template"):
        templates[t.args[0]] = [t.attrs[k] for k in ("variable_1", "variable_2") if k in t.attrs]
    return {g.args[0]: Cell(g, templates) for g in lib.find("cell")}


def wire_cap(lib: Group, fanout: int) -> float:
    wl = next(g for g in lib.find("wire_load") if g.args[0] == WIRE_LOAD)
    points = sorted((int(a), float(b)) for a, b in wl.complex["fanout_length"])
    if fanout <= points[0][0]:
        length = points[0][1] * fanout / points[0][0]
    elif fanout >= points[-1][0]:
        length = points[-1][1] + (fanout - points[-1][0]) * float(wl.attrs["slope"])
    else:
        j = max(i for i, (f, _) in enumerate(points) if f <= fanout)
        (f0, l0), (f1, l1) = points[j], points[j + 1]
        length = l0 + (fanout - f0) * (l1 - l0) / (f1 - f0)
    return length * float(wl.attrs["capacitance"])


# ---- timing ------------------------------------------------------------------------------------------------

MAX_FANOUT = 8           # the liberty's default_max_fanout; the layout flow buffers above it
BUFFER = "sg13cmos5l_buf_4"
CLOCK_SLEW = 0.15        # ns, an assumed clock transition at every register


def bit_name(port: str, index: int, width: int) -> str:
    return port if width == 1 else "%s[%d]" % (port, index)


def analyse(netlist: dict, std: Group, sram_lib: Group) -> None:
    lib_cells = cells_of(std)
    sram = cells_of(sram_lib)[SRAM]
    lib_cells[SRAM] = sram
    module = next(m for m in netlist["modules"].values() if m.get("attributes", {}).get("top"))
    cells = module["cells"]

    drivers: Dict[int, Tuple[str, str]] = {}
    sinks: Dict[int, List[Tuple[str, str]]] = {}
    pins: Dict[str, Dict[str, int]] = {}
    for cname, c in cells.items():
        if c["type"] not in lib_cells:
            raise SystemExit("no liberty cell for %s (%s)" % (cname, c["type"]))
        pins[cname] = {}
        for port, bits in c["connections"].items():
            for i, b in enumerate(bits):
                if not isinstance(b, int):
                    continue  # a constant
                name = bit_name(port, i, len(bits))
                pins[cname][name] = b
                if c["port_directions"][port] == "output":
                    drivers[b] = (cname, name)
                else:
                    sinks.setdefault(b, []).append((cname, name))

    def base(pin: str) -> str:
        return re.sub(r"\[\d+\]$", "", pin)

    def pin_cap(cname: str, pin: str) -> float:
        cell = lib_cells[cells[cname]["type"]]
        return cell.cap.get(pin, cell.cap.get(base(pin), 0.0))

    # A net with more than MAX_FANOUT sinks is driven through a tree of buffers, MAX_FANOUT per level:
    # the driver sees MAX_FANOUT buffer inputs, and each level adds one buffer's delay.
    buf = lib_cells[BUFFER]
    buf_arc = buf.arcs["X"][0]
    load: Dict[int, float] = {}
    tree: Dict[int, Tuple[float, float]] = {}  # net -> (extra delay, slew at the sinks)
    for b in set(drivers) | set(sinks):
        fan = sinks.get(b, [])
        caps = [pin_cap(c, p) for c, p in fan]
        if len(fan) <= MAX_FANOUT:
            load[b] = sum(caps) + wire_cap(std, len(fan))
            continue
        levels = math.ceil(math.log(len(fan)) / math.log(MAX_FANOUT)) - 1
        inner = MAX_FANOUT * buf.cap["A"] + wire_cap(std, MAX_FANOUT)
        leaf = MAX_FANOUT * (sum(caps) / len(caps)) + wire_cap(std, MAX_FANOUT)
        load[b] = inner
        delay, slew = 0.0, 0.3
        for level in range(levels):
            c_out = leaf if level == levels - 1 else inner
            delay += max(t.at(input_net_transition=slew, total_output_net_capacitance=c_out) for t in buf_arc.delay)
            slew = max(t.at(input_net_transition=slew, total_output_net_capacitance=c_out) for t in buf_arc.slew)
        tree[b] = (delay, slew)

    def is_reg(cname: str) -> bool:
        t = cells[cname]["type"]
        return t == SRAM or "df" in t or "dl" in t

    def is_source(cname: str) -> bool:
        return is_reg(cname) or "tie" in cells[cname]["type"]

    def arrive(b: int, t: float, s: float) -> Tuple[float, float]:
        extra = tree.get(b)
        return (t + extra[0], extra[1]) if extra else (t, s)

    def launches(sources: str) -> Dict[int, Tuple[float, float, Optional[int]]]:
        arr: Dict[int, Tuple[float, float, Optional[int]]] = {}
        for b, (cname, pin) in drivers.items():
            kind = cells[cname]["type"]
            if kind == SRAM and sources in ("sram", "all"):
                arc = next(a for a in sram.arcs[base(pin)] if a.kind == "rising_edge")
            elif "df" in kind and sources == "all":
                arc = next(a for a in lib_cells[kind].arcs[pin] if a.kind == "rising_edge")
            else:
                continue
            t = max(d.at(input_net_transition=CLOCK_SLEW, total_output_net_capacitance=load[b]) for d in arc.delay)
            s = max(f.at(input_net_transition=CLOCK_SLEW, total_output_net_capacitance=load[b]) for f in arc.slew)
            arr[b] = arrive(b, t, s) + (None,)
        return arr

    # Combinational cells in topological order (depth first from every cell).
    order: List[str] = []
    state: Dict[str, int] = {}
    for root in cells:
        if is_source(root):
            continue
        stack = [(root, False)]
        while stack:
            cname, done = stack.pop()
            if done:
                state[cname] = 2
                order.append(cname)
                continue
            if state.get(cname):
                continue
            state[cname] = 1
            stack.append((cname, True))
            for pin, b in pins[cname].items():
                if b in drivers and lib_cells[cells[cname]["type"]].direction.get(base(pin)) == "input":
                    prev = drivers[b][0]
                    if is_source(prev) or state.get(prev) == 2:
                        continue
                    if state.get(prev) == 1:
                        raise SystemExit("combinational loop through %s" % prev)
                    stack.append((prev, False))

    def propagate(sources: str) -> Dict[int, Tuple[float, float, Optional[int]]]:
        arr = launches(sources)
        for cname in order:
            cell = lib_cells[cells[cname]["type"]]
            for out_pin, arcs in cell.arcs.items():
                if cell.direction.get(out_pin) != "output" or out_pin not in pins[cname]:
                    continue
                ob = pins[cname][out_pin]
                best = None
                for arc in arcs:
                    ib = pins[cname].get(arc.related)
                    if ib is None or ib not in arr or not arc.delay:
                        continue
                    t_in, s_in, _ = arr[ib]
                    t = t_in + max(d.at(input_net_transition=s_in, total_output_net_capacitance=load[ob])
                                   for d in arc.delay)
                    s = max(f.at(input_net_transition=s_in, total_output_net_capacitance=load[ob]) for f in arc.slew)
                    if best is None or t > best[0]:
                        best = (t, s, ib)
                if best is not None:
                    arr[ob] = arrive(ob, best[0], best[1]) + (best[2],)
        return arr

    def setup(cname: str, pin: str, slew: float) -> float:
        cell = lib_cells[cells[cname]["type"]]
        arcs = cell.arcs.get(pin) or cell.arcs.get(base(pin)) or []
        values = [tbl.at(constrained_pin_transition=slew, related_pin_transition=CLOCK_SLEW)
                  for a in arcs if a.kind == "setup_rising" for tbl in a.constraint]
        return max(values) if values else 0.0

    def report(title: str, arr, endpoint) -> None:
        worst = None
        for b, lst in sinks.items():
            if b not in arr:
                continue
            for cname, pin in lst:
                if endpoint(cname, pin):
                    t, s, _ = arr[b]
                    need = t + setup(cname, pin, s)
                    if worst is None or need > worst[0]:
                        worst = (need, t, cname, pin, b)
        print(title)
        if worst is None:
            print("  no path")
            return
        need, t, cname, pin, b = worst
        path = []
        first = b
        while b is not None:
            when, _, prev = arr[b]
            dname, dpin = drivers[b]
            fan = len(sinks.get(b, []))
            path.append("%7.3f ns  %-24s fanout %-3d %s.%s" % (when, cells[dname]["type"], fan, dname, dpin))
            first, b = b, prev
        path.reverse()
        launch = arr[first][0]
        print("  launch, clock to %s output: %.3f ns" % (cells[drivers[first][0]]["type"], launch))
        print("  logic: %.3f ns through %d cells (buffer trees on fanouts above %d included)"
              % (t - launch, len(path) - 1, MAX_FANOUT))
        print("  setup at %s.%s: %.3f ns" % (cells[cname]["type"], pin, need - t))
        print("  needs %.3f ns of the %.1f ns clock: slack %.3f ns before wires, skew and margin"
              % (need, CLOCK_NS, CLOCK_NS - need))
        for line in path:
            print("    " + line)

    report("Decision loop, SRAM A_DOUT -> SRAM A_ADDR:", propagate("sram"),
           lambda c, p: cells[c]["type"] == SRAM and p.startswith("A_ADDR"))
    report("Worst path in the chip, any register -> any register:", propagate("all"),
           lambda c, p: is_reg(c) and (p == "D" or cells[c]["type"] == SRAM))


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    netlist = json.load(open(sys.argv[1]))
    analyse(netlist, parse_liberty(sys.argv[2]), parse_liberty(sys.argv[3]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

