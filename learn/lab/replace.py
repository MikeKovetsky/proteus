# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The proof of becoming, on two chips in Icarus (learn/lab/pair).

The controller is a shipped program, unmodified, on one chip, with the test written for the device. The test and
300 random transactions run against the device. Then the device is taken away, the learned program is loaded
into a second chip through its pins, and the same run has to give the same answers. The same run against
nothing, and against programs with one wrong byte, has to fail."""

import copy
import json
import random
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .board import BUILD, ROOT
from .board import jobs as default_jobs

PAIR = Path(__file__).resolve().parent / "pair"
CLOCK_PS = {"spi": 41670, "i2c": 16668}  # 24 MHz makes the shipped 5 MHz SPI program a 2 MHz controller
TOO_FAST_PS = 16668  # the same program as it ships
CONTROLLER = {"spi": "spi_ctrl_m0", "i2c": "i2c_ctrl"}
TRAFFIC = 300

# The tests written for the devices, from their descriptions in devices.py: what is sent, what must come back.
FLASH_TEST = (
    ("identity", "9F 00 00 00 00", "FF C7 3A 14 00"),
    ("status, repeated", "05 00 00", "FF 00 00"),
    ("write enable", "06", "FF"),
    ("status shows it", "05 00", "FF 02"),
    ("write disable", "04", "FF"),
    ("status", "05 00", "FF 00"),
    ("write enable with a byte too many", "06 00", "FF FF"),
    ("is ignored", "05 00", "FF 00"),
    ("the test mode's command, not write enabled", "5E", "FF"),
    ("is ignored", "05 00", "FF 00"),
    ("write enable", "06", "FF"),
    ("the hidden test mode", "5E", "FF"),
    ("status shows it", "05 00", "FF 80"),
    ("a fourth identity byte", "9F 00 00 00 00 00", "FF C7 3A 14 2C 00"),
    ("the hidden key", "E7 00 00 00 00 00", "FF 4D 4B 56 31 00"),
    ("arm a reset", "66", "FF"),
    ("status shows it", "05 00", "FF C0"),
    ("which disarmed it", "05 00", "FF 80"),
    ("a reset that is not armed", "99", "FF"),
    ("is ignored", "05 00", "FF 80"),
    ("arm", "66", "FF"),
    ("reset", "99", "FF"),
    ("out of test mode", "05 00", "FF 00"),
    ("no key outside it", "E7 00 00", "FF FF FF"),
    ("sleep", "B9", "FF"),
    ("asleep", "05 00", "FF FF"),
    ("asleep, no identity", "9F 00", "FF FF"),
    ("wake", "AB", "FF"),
    ("awake", "05 00", "FF 00"),
)
SENSOR_TEST = (
    ("identity", "4B: 0F / 1", "47 ok"),
    ("temperature in standby", "4B: 00 / 2", "80 00 ok"),
    ("a read with no pointer", "4B: / 1", "80 ok"),
    ("status in standby", "4B: 02 / 1", "10 ok"),
    ("no second address", "4F: / 1", "nack"),
    ("the hidden write, in standby", "4B: 7E C3 / 0", "ok"),
    ("does nothing", "4B: 02 / 1", "10 ok"),
    ("run", "4B: 01 01 / 0", "ok"),
    ("temperature", "4B: 00 / 3", "19 60 FF ok"),
    ("status while running", "4B: 02 / 1", "11 ok"),
    ("a value control refuses", "4B: 01 02 / 0", "nack"),
    ("a read-only register", "4B: 0F 00 / 0", "nack"),
    ("the hidden register, shut", "4B: 7E / 1", "FF ok"),
    ("the hidden write", "4B: 7E C3 / 0", "ok"),
    ("service mode", "4B: 02 / 1", "91 ok"),
    ("the hidden register", "4B: 7E / 1", "A5 ok"),
    ("the hidden second address", "4F: / 3", "5E 1D FF ok"),
    ("another address", "22: / 1", "nack"),
    ("standby", "4B: 01 00 / 0", "ok"),
    ("out of service", "4B: 02 / 1", "10 ok"),
    ("the second address is gone", "4F: / 0", "nack"),
)
THERMOMETER_TEST = (
    ("temperature", "48: 00 / 2", "19 80 ok"),
    ("configuration", "48: 01 / 2", "60 FF ok"),
    ("the pointer stays", "48: / 1", "60 ok"),
    ("no third register", "48: 05 / 0", "nack"),
    ("another address", "49: / 1", "nack"),
)
TESTS = {"flash": FLASH_TEST, "sensor": SENSOR_TEST, "thermometer": THERMOMETER_TEST}
BUS = {"flash": "spi", "sensor": "i2c", "thermometer": "i2c"}
# A byte of each program that only a hidden feature shows, for the broken programs: one sent, one compared
HIDDEN_BYTE = {"flash": (0x56, 0x5E), "sensor": (0xA5, 0xC3), "thermometer": (0x60, 0x01)}
OK, NACKED = 0x00, 0x01


def hexes(text):
    return [int(word, 16) for word in text.split()]


def request(bus, sent):
    """What the shipped controller's host writes, from a line of a test."""
    if bus == "spi":
        data = hexes(sent)
        return [len(data)] + data
    address, rest = sent.split(":")
    writes, reads = rest.split("/")
    data = hexes(writes)
    return [int(address, 16), len(data)] + data + [int(reads)]


def answer(bus, wanted):
    if bus == "spi":
        return [hexes(wanted), []]
    words = wanted.split()
    return [hexes(" ".join(words[:-1])), [OK if words[-1] == "ok" else NACKED]]


def traffic(part, count=TRAFFIC, seed=1):
    """Random transactions that lean towards what the device acts on."""
    from . import devices

    rng = random.Random("%s %d" % (part, seed))
    out = []
    if part == "flash":
        known = devices.Flash.documented + devices.Flash.hidden
        for _ in range(count):
            first = rng.choice(known) if rng.random() < 0.85 else rng.randrange(256)
            data = [first] + [rng.randrange(256) for _ in range(rng.choice((0, 0, 0, 1, 1, 2, 5)))]
            out.append([len(data)] + data)
        return out
    ours = (0x4B, 0x4B, 0x4B, 0x4F) if part == "sensor" else (0x48, 0x48, 0x49)
    known = devices.Sensor.documented + devices.Sensor.hidden if part == "sensor" else (0x00, 0x00, 0x01, 0x01, 0x02)
    for _ in range(count):
        address = rng.choice(ours) if rng.random() < 0.95 else rng.randrange(128)
        data = [rng.choice(known) if rng.random() < 0.9 else rng.randrange(256)
                for _ in range(rng.choice((0, 1, 1, 1, 1, 1, 2, 3)))]
        out.append([address, len(data)] + data + [rng.choice((0, 1, 1, 2, 3))])
    return out


def broken(image, byte, compared):
    """The image with one byte of one kind of entry wrong, in every copy of it."""
    data, changed = bytearray(image), 0
    start = 1 + 16 * image[0] + 2
    for at in range(start, len(data), 8):
        a = int.from_bytes(data[at:at + 4], "little")
        b = int.from_bytes(data[at + 4:at + 8], "little")
        if compared:
            chosen = a >> 9 & 15 == 15
        else:
            chosen = b >> 19 & 7 == 1 and b >> 28 & 7 == 6
        if chosen and b & 0xFF == byte:
            data[at + 4] ^= 1
            changed += 1
    if not changed:
        raise ValueError("no entry holds %02X" % byte)
    return bytes(data)


class Pair:
    """The two-chip simulation, built once into `work`."""

    def __init__(self, work=None):
        from cocotb_tools.runner import get_runner

        self.own = work is None
        if self.own:
            BUILD.mkdir(parents=True, exist_ok=True)
            work = tempfile.mkdtemp(prefix="pair-", dir=str(BUILD))
        self.work = Path(work)
        if str(ROOT) not in sys.path:
            sys.path.append(str(ROOT))  # the simulator's Python gets this process's path
        self.runner = get_runner("icarus")
        sources = [PAIR / "tb_pair.v"] + [ROOT / "src" / name for name in (
            "project.v", "protoemu_core.v", "protoemu_fifo.v", "protoemu_table.v")]
        sources += sorted((ROOT / "test" / "ihp_sram").glob("*.v"))
        self.runner.build(sources=sources, hdl_toplevel="tb_pair", build_dir=self.work / "build",
                          defines={"FUNCTIONAL": 1}, always=True, timescale=("1ns", "1ps"),
                          log_file=self.work / "build.log")

    def close(self):
        if self.own:
            shutil.rmtree(str(self.work), ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def run(self, label, cfg):
        here = self.work / label
        here.mkdir(parents=True, exist_ok=True)
        cfg = dict(cfg, result=str(here / "result.json"))
        (here / "run.json").write_text(json.dumps(cfg))
        results = copy.deepcopy(self.runner).test(  # it keeps state, so one a thread
            test_module="learn.lab.pair.pair_bench", hdl_toplevel="tb_pair", build_dir=self.work / "build",
            test_dir=here, results_xml=str(here / "results.xml"), log_file=here / "run.log",
            extra_env={"LEARN_PAIR": str(here / "run.json")})
        tree = ElementTree.parse(str(results))
        if not list(tree.iter("testcase")) or list(tree.iter("failure")) or list(tree.iter("error")):
            raise RuntimeError("the %s run did not finish: see %s" % (label, here / "run.log"))
        return json.loads((here / "result.json").read_text())


def replacement(pair, part, image, count=TRAFFIC, jobs=None):
    """Every run for one device, and what they add up to."""
    bus, test = BUS[part], TESTS[part]
    requests = [request(bus, sent) for _, sent, _ in test]
    base = {"bus": bus, "part": part, "program": CONTROLLER[bus], "clock_ps": CLOCK_PS[bus]}
    whole = dict(base, requests=requests + traffic(part, count))
    byte, compared = HIDDEN_BYTE[part]
    runs = {
        "device": dict(whole, answers_with="device"),
        "clone": dict(whole, answers_with="chip", image=image.hex()),
        "nothing": dict(base, requests=requests, answers_with="nothing"),
        "broken sent": dict(base, requests=requests, answers_with="chip", image=broken(image, byte, False).hex()),
        "broken compare": dict(base, requests=requests, answers_with="chip",
                               image=broken(image, compared, True).hex()),
    }
    if bus == "spi":
        sent = [hexes(line) for _, line, _ in test]
        runs["tester, device"] = dict(base, tester=sent, answers_with="device")
        runs["tester, clone"] = dict(base, tester=sent, answers_with="chip", image=image.hex())
        runs["too fast, device"] = dict(base, requests=requests, answers_with="device", clock_ps=TOO_FAST_PS)
        runs["too fast, clone"] = dict(base, requests=requests, answers_with="chip", image=image.hex(),
                                       clock_ps=TOO_FAST_PS)
    with ThreadPoolExecutor(jobs or default_jobs()) as pool:
        labels = list(runs)
        got = dict(zip(labels, pool.map(
            lambda label: pair.run("%s-%s" % (part, label.replace(",", "").replace(" ", "-")), runs[label]), labels)))
    return summary(part, test, got, len(whole["requests"]))


def summary(part, test, got, total):
    bus = BUS[part]
    wanted = [answer(bus, wanted) for _, _, wanted in test]

    def test_passes(run):
        return run["answers"][:len(wanted)] == wanted and not run["faults"] and not run.get("bus_check")

    device, clone = got["device"], got["clone"]
    out = {
        "part": part,
        "transactions": total,
        "bytes": sum(len(answer[0]) for answer in device["answers"] if isinstance(answer, list)),
        "test passes on the device": test_passes(device),
        "test passes on the clone": test_passes(clone),
        "same answers": device["answers"] == clone["answers"] and len(device["answers"]) == total,
        "test fails on nothing": not test_passes(got["nothing"]),
        "test fails on a wrong byte sent": not test_passes(got["broken sent"]),
        "test fails on a wrong compare": not test_passes(got["broken compare"]),
        "delays": {"device": device["delays"], "clone": clone["delays"]},
        "problems": [got[k]["faults"] + [got[k].get("bus_check")] for k in ("device", "clone")
                     if got[k]["faults"] or got[k].get("bus_check")],
    }
    if bus == "i2c":  # on SPI the answers are all that the target puts on the bus
        out["same bus"] = bool(device.get("decoded")) and device["decoded"] == clone.get("decoded")
    if bus == "spi":
        sent = [answer[0] for answer in wanted]
        for who in ("device", "clone"):
            run = got["tester, " + who]
            out["test/protocols/spi.py passes the " + who] = run["answers"] == sent and not run["faults"]
        out["at 5 MHz the device passes"] = test_passes(got["too fast, device"])
        out["at 5 MHz the clone passes"] = test_passes(got["too fast, clone"])  # it is not expected to
    out["replaced"] = all(out[key] for key in out if key.startswith(("test", "same")))
    return out
