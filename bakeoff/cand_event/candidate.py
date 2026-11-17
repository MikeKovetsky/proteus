"""The bake-off's entry points for the event-machine candidate."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .asm import assemble
from .machine import EventChip

NAME = "event"
PROGRAMS = Path(__file__).resolve().parent / "programs"


def make_chip() -> EventChip:
    return EventChip()


def program(bench_name: str) -> Optional[bytes]:
    source = PROGRAMS / ("%s.s" % bench_name)
    if not source.exists():
        return None
    return assemble(source.read_text())
