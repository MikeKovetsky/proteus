"""cand_pio: PIO-style state machines with a tick-synchronised instruction set. See README.md."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .asm import assemble
from .chip import PioChip

NAME = "pio"
PROGRAMS = Path(__file__).resolve().parent / "programs"


def make_chip() -> PioChip:
    return PioChip()


def program(bench_name: str) -> Optional[bytes]:
    source = PROGRAMS / ("%s.pio" % bench_name)
    if not source.exists():
        return None
    return assemble(source.read_text())
