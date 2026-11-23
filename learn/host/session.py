# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""One run: from what the host is told and a link to the chip, to a program or a refusal.

The link has two calls, and they are the host's only way to the device:
    load(image) -> True when the chip runs
    exchange(bytes for channel 0, how many to read) -> (what came back on channel 1, what was on channel 2)

`told` is everything else the host knows. It is plain numbers about the board: never anything of the device's
behaviour."""

from .bus import Refused
from .i2c import I2c
from .learner import Learner
from .search import DEEPEST, PAIRS, Search
from .spi import Spi

BUSES = {"spi": Spi, "i2c": I2c}
RATE = {"spi": 2500, "i2c": 400}
TOLD = {
    "bus": None,           # spi or i2c
    "rate": None,          # kHz of the questions
    "serve": None,         # kHz of the controller that the clone will face, if not the same
    "reset_us": 1,         # how long nRESET is held low
    "boot_us": 5,          # and how long the device then needs
    "inputs": None,        # the only symbols to learn, where every other is out of scope
    "extra": 1,            # m: states beyond those found that the depth suite must cover
    "max_states": None,    # or the most states the device has, phase included
    "effort": 60000,       # bus symbols the depth suite may take to cover more than `extra`
    "budget": 6000000,     # bus symbols in all, after which nothing is written
    "quiet_us": 200,       # the audit's pauses. 0 leaves them out
    "hold_us": 5000,
    "seed": 1,
    "walks": 300,
    "limit_states": 240,
    "limit_alphabet": 64,
}
LEAST = {"reset_us": 1, "rate": 1, "serve": 1, "max_states": 1, "budget": 1, "limit_states": 1, "limit_alphabet": 1}
MOST = {"reset_us": 255, "boot_us": 255, "extra": DEEPEST}


def settings(told):
    out = dict(TOLD)
    for key in told:
        if key not in TOLD:
            raise Refused("told", "nothing called %r can be told" % (key,))
        out[key] = told[key]
    if out["bus"] not in BUSES:
        raise Refused("told", "the bus must be named: spi or i2c")
    if out["inputs"] is not None and (type(out["inputs"]) not in (list, tuple) or not out["inputs"]):
        raise Refused("told", "inputs is a list of symbols, or is not told")
    if out["rate"] is None:
        out["rate"] = RATE[out["bus"]]
    for key in TOLD:
        value = out[key]
        if key in ("bus", "inputs") or (value is None and TOLD[key] is None):
            continue
        if type(value) is not int or value < LEAST.get(key, 0) or (key in MOST and value > MOST[key]):
            raise Refused("told", "%s cannot be %r" % (key, value))
    return out


def exported(model, bus):
    rows = []
    for q in range(model.n):
        row = model.rows[q]
        rows.append([(a, row[a][0], row[a][1]) for a in model.alphabet if a in row])
    return {"phases": list(model.phases), "alphabet": list(model.alphabet), "defaults": list(model.defaults),
            "inputs": None if bus.inputs is None else list(bus.inputs), "rows": rows}


def guarantee(told, bus, model, search):
    n, m = model.n, search.extra
    lines = ["Both lines assume a device that answers the same way every time after a reset, whose state changes "
             "only with whole symbols on this bus, and whose states are counted with the bus phase as part of "
             "them."]
    if bus.inputs is None:
        lines.append("If the device has at most %d states, the model is exact for every symbol the bus allows "
                     "(Wp-method, no extra state, every byte value tried)." % n)
        lines.append("If it has at most %d states, and treats each symbol not learned in full as it treats that "
                     "kind's default, the model is exact (Wp-method, %d extra states over %d symbols)."
                     % (n + m, m, len(model.alphabet)))
    else:
        lines.append("If the device has at most %d states, the model is exact for the %d inputs the host was "
                     "told of, and says nothing about any other (Wp-method, %d extra states)."
                     % (n + m, len(bus.inputs), m))
    return lines


def warnings(told, bus, model, search):
    lines = ["Bounded, not proven: behaviour that needs more states than that, or more steps, can be missing, "
             "and no test through the pins alone can rule it out."]
    cover = search.pairs_cover
    if bus.inputs is None and cover and cover[0] == cover[1]:
        lines.append("A byte not learned in full was tried singly from each state, and every two bytes were tried "
                     "at the start of a transaction, from each state that waits for one, %d openings to a "
                     "question. A secret that shows nothing until its third byte, that starts after a "
                     "transaction's first byte, that spans transactions, or that a later opening in its question "
                     "undoes, can be missing." % PAIRS)
    elif bus.inputs is None:
        lines.append("A symbol not learned in full was tried singly from each state. A secret that starts with "
                     "such a symbol and shows nothing before its next step can be missing.")
    else:
        lines.append("Only the inputs the host was told of were tried. The clone halts on any other, and puts "
                     "EE on channel 2.")
    lines.append("A device that a reset does not clear, or that remembers what came before it, can be copied "
                 "wrongly. The questions were repeated in another order, which catches some such devices.")
    lines.append("Questioning sends every byte value: on a real device that can write, erase or lock it.")
    if search.checked:
        lines.append("The last pass compared %d answers and all agreed. If wrong answers came independently, "
                     "their rate is below 1 in %d at 95%% confidence. Faults that depend on history or time are "
                     "not bounded by that." % (search.checked, max(search.checked // 3, 1)))
    lines.append("The program comes from a generator that is tested and not proven. Compare it with the device "
                 "on the controller it will face before relying on it.")
    if bus.serve != bus.rate:
        lines.append("The questions ran at %d kHz and the program is to serve %d kHz. Nothing was asked at that "
                     "rate." % (bus.rate, bus.serve))
    lines.append("Timing, partial bytes and other devices' traffic were not learned. The questions ran at %d kHz, "
                 "and pauses of up to %d us were tried." % (bus.rate, max(told["quiet_us"], told["hold_us"])))
    return lines


def flags(told, bus, model, search):
    """[(code, line)]: what this run saw that puts its copy more at risk than another's. The warnings above hold
    for every copy, so they cannot tell one copy from another. These come from the run alone."""
    out = []
    late = sum(search.found_by.get(stage, 0) for stage in ("pairs", "audit"))
    if late:
        out.append(("found outside the bounds",
                    "%d counterexample%s turned up after both bounded suites had passed on the model of the time, "
                    "in the pairs search or the audit. This device does what the bounds do not cover, so more of "
                    "that kind can be missing." % (late, "" if late == 1 else "s")))
    cover = search.pairs_cover
    if cover and cover[0] < cover[1]:
        out.append(("pairs cut short",
                    "The budget ran out before the pairs search had tried every two-byte opening from every state "
                    "that waits for a transaction: %d of %d questions were asked. A command with an argument can "
                    "be missing." % cover))
    return out


def needs(told, bus):
    """What the host had to be told or given, for a person to check before trusting the program."""
    lines = ["The bus and its wiring: %s, on %s." % (told["bus"], bus.WIRING),
             "The rates: questions at %d kHz, and a controller at up to %d kHz for the program to face."
             % (bus.rate, bus.serve),
             "A reset on P4, low to reset, that returns the device to the same state every time: held %d us, "
             "with %d us to recover." % (told["reset_us"], told["boot_us"]),
             "The bus to itself while it asks: no controller and no other device driving it.",
             "Leave to write to the device, since questions can change it."]
    if bus.inputs is not None:
        lines.append("The %d inputs that matter. Every other is out of scope." % len(bus.inputs))
    if told["max_states"] is not None:
        lines.append("The most states the device has: %d, the bus phase counted." % told["max_states"])
    return lines


def run(told, link):
    """Plain data, always: either a program and what is known about it, or no program and why."""
    result = {"ok": False, "code": "ok", "reason": "", "image": None, "model": None, "stats": {},
              "guarantee": [], "warnings": [], "flags": [], "differs": [], "told": None, "wiring": "", "needs": []}
    learner = search = None
    try:
        told = settings(told)
        bus = BUSES[told["bus"]](told)
        result["told"], result["wiring"] = told, bus.WIRING
        result["needs"] = needs(told, bus)
        if not link.load(bus.image()):
            raise Refused("link", "the query engine did not load")
        learner = Learner(bus, link, told)
        search = Search(learner)
        model = learner.learn(search)
        result["model"] = exported(model, bus)
        result["guarantee"] = guarantee(told, bus, model, search)
        result["warnings"] = warnings(told, bus, model, search)
        found = flags(told, bus, model, search)
        result["flags"] = [code for code, _ in found]
        result["warnings"][1:1] = [line for _, line in found]
        nothing = bus.absent(model)
        if nothing:
            raise Refused("nothing", nothing + ". There is nothing to copy")
        clone = bus.clone(model)
        result["differs"] = clone["differs"]
        result["stats"].update(entries=clone["entries"], chain=clone["chain"])
        if clone["image"] is None:
            raise Refused("table", "the model needs %d table entries and the chip has 512" % clone["entries"])
        result["image"], result["ok"] = clone["image"], True
    except Refused as refusal:
        result["code"], result["reason"] = refusal.code, refusal.text
    except Exception as fault:
        result["code"] = "error"
        result["reason"] = "the host failed (%s: %s), so no program is written" % (type(fault).__name__, fault)
    if learner is not None:
        result["stats"].update(states=len(learner.access), symbols=learner.symbols, queries=learner.queries,
                               counterexamples=learner.counterexamples, alphabet=len(learner.alphabet))
        if search is not None:
            result["stats"].update(found_by=dict(search.found_by), extra=search.extra,
                                   tests_last_pass=dict(search.report), checked_last_pass=search.checked,
                                   pairs=search.pairs_cover)
    return result
