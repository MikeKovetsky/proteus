"""The contract between the protocol benches and the candidate architectures.

One call to `Chip.step` is one rising clock edge at CLK_HZ. The pad ring is modelled by `Sim`, not by the
candidate, so every candidate pays the same latency: two cycles in (the synchroniser) and one cycle out (the
output register).
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

CLK_HZ = 60_000_000
N_PINS = 8  # protocol pins P0..P7: the chip's eight bidirectional pads

# Byte FIFOs between the host and the chip. The host link moves at most one byte per channel, in each
# direction, every HOST_CYCLES_PER_BYTE cycles.
HOST_CHANNELS = 4
HOST_CYCLES_PER_BYTE = 16
CH_TX = 0      # host -> chip: what to send, in the format the scenario gives
CH_RX = 1      # chip -> host: what was received
CH_STATUS = 2  # chip -> host: status bytes, as the scenario gives
CH_AUX = 3     # host -> chip: spare


class Chip:
    """A cycle-accurate model of one candidate architecture."""

    name = "unnamed"

    def reset(self) -> None:
        """Return to the state `rst_n` gives. Program memory does not survive it."""
        raise NotImplementedError

    def load(self, image: bytes) -> None:
        """Load a program image, as the loader would right after reset."""
        raise NotImplementedError

    def step(self, pins: int) -> Tuple[int, int]:
        """Advance one clock.

        `pins` is the synchronised level of P0..P7 (bit n is Pn). Return `(out, oe)`: the level the chip
        wants on each pin, and which pins it drives. Both take effect on the pads one cycle later.
        """
        raise NotImplementedError

    def host_can_push(self, ch: int) -> bool:
        """True when the chip's FIFO for host -> chip channel `ch` has room for one byte."""
        raise NotImplementedError

    def host_push(self, ch: int, byte: int) -> None:
        """The host writes one byte into channel `ch`. Called only after `host_can_push(ch)`."""
        raise NotImplementedError

    def host_pop(self, ch: int) -> Optional[int]:
        """The host reads one byte from chip -> host channel `ch`, or None when it is empty."""
        raise NotImplementedError

    def budget(self) -> Dict[str, object]:
        """What the candidate would cost in hardware.

        Keys: `flops` (every flip-flop bit, FIFOs included), `sram_bits`, `gates_estimate` (combinational
        cells), `engines` (how many protocols can run at once) and `notes`.
        """
        raise NotImplementedError
