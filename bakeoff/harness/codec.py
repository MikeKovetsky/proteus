"""Reference encoders and checks the benches judge against: CRCs, bit stuffing, NRZI and Manchester.

They are small pure functions, and the self-test checks each against a known vector.
"""
from __future__ import annotations

from typing import List, Optional, Tuple


def crc8_onewire(data: bytes) -> int:
    """The 1-Wire CRC-8: x^8 + x^5 + x^4 + 1, reflected (0x8C), initial value 0."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


def crc16_usb_register(data: bytes, crc: int = 0xFFFF) -> int:
    """The raw CRC-16 register (polynomial 0x8005, reflected 0xA001) after `data`, before the final inversion.

    Run over a packet's data and its CRC together, a good packet leaves the residual 0xB001.
    """
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


USB_CRC16_RESIDUAL = 0xB001


def crc16_usb(data: bytes) -> int:
    """The USB data CRC-16: initial value all ones, inverted at the end. It goes on the wire low byte first."""
    return crc16_usb_register(data) ^ 0xFFFF


def crc32_eth(data: bytes) -> int:
    """The Ethernet FCS: polynomial 0x04C11DB7, reflected (0xEDB88320), initial value all ones, inverted.

    It goes on the wire low byte first.
    """
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
    return crc ^ 0xFFFFFFFF


def usb_stuff(bits: List[int]) -> List[int]:
    """Insert a 0 after every run of six 1s."""
    out: List[int] = []
    ones = 0
    for bit in bits:
        out.append(bit)
        ones = ones + 1 if bit else 0
        if ones == 6:
            out.append(0)
            ones = 0
    return out


def usb_unstuff(bits: List[int]) -> Tuple[List[int], Optional[int]]:
    """Drop the 0 after every run of six 1s. Also return the index of the first stuffing error, or None.

    A seventh 1 in a row is an error. A stream that ends right after six 1s is accepted.
    """
    out: List[int] = []
    ones = 0
    error: Optional[int] = None
    for index, bit in enumerate(bits):
        if ones == 6:
            if bit and error is None:
                error = index
            ones = 0
            continue
        out.append(bit)
        ones = ones + 1 if bit else 0
    return out, error


def nrzi_encode(bits: List[int], level: int = 1) -> List[int]:
    """USB's NRZI: a 0 toggles the line and a 1 leaves it. `level` is the line before the first bit (1 is J)."""
    out: List[int] = []
    for bit in bits:
        if not bit:
            level ^= 1
        out.append(level)
    return out


def nrzi_decode(levels: List[int], level: int = 1) -> List[int]:
    """Undo `nrzi_encode`: a change of level is a 0."""
    out: List[int] = []
    for now in levels:
        out.append(1 if now == level else 0)
        level = now
    return out


USB_SYNC_BITS = [0, 0, 0, 0, 0, 0, 0, 1]  # as line states: K J K J K J K K


def usb_packet_levels(body: bytes) -> List[int]:
    """The J (1) and K (0) states of a packet, one per bit time: SYNC, then `body` LSB first, stuffed."""
    bits = USB_SYNC_BITS + [(byte >> b) & 1 for byte in body for b in range(8)]
    return nrzi_encode(usb_stuff(bits))


def manchester_encode(bits: List[int]) -> List[int]:
    """10BASE-T Manchester as half-bit levels: a 1 is low then high, a 0 is high then low."""
    out: List[int] = []
    for bit in bits:
        out += [0, 1] if bit else [1, 0]
    return out


def eth_frame_bytes(frame: bytes) -> bytes:
    """What goes on the wire for `frame`: seven 0x55, 0xD5, the frame, and its FCS low byte first."""
    return b"\x55" * 7 + b"\xd5" + frame + crc32_eth(frame).to_bytes(4, "little")
