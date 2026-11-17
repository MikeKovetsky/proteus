"""The table-driven candidate: a state table in SRAM, compiled from a state-machine language."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from .chip import TableChip
from .compiler import compile_file

NAME = "table"
PROGRAMS = Path(__file__).resolve().parent / "programs"
_images: Dict[str, bytes] = {}


def make_chip() -> TableChip:
    return TableChip()


def program(bench_name: str) -> Optional[bytes]:
    """Compile `programs/<bench_name>.fsm` to a table image, or return None if there is no such source."""
    if bench_name not in _images:
        source = PROGRAMS / ("%s.fsm" % bench_name)
        if not source.exists():
            return None
        _images[bench_name] = compile_file(source).image
    return _images[bench_name]
