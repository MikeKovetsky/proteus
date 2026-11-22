# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""The three CRCs, from the parameters in the RevEng catalogue (reveng.sourceforge.io/crc-catalogue), which also
gives the check values and the codewords below.

The register shifts left with the polynomial as the standards print it and is reflected at the end. The chip's
LFSR shifts right with reflected constants, so the two share neither constants nor form.
"""

from .wave import bits_lsb

# name: width, poly, init, xorout (all three take bits LSB first and reflect the result)
CRC8_MAXIM = (8, 0x31, 0x00, 0x00)  # 1-Wire
CRC16_USB = (16, 0x8005, 0xFFFF, 0xFFFF)
CRC32_ETHERNET = (32, 0x04C11DB7, 0xFFFFFFFF, 0xFFFFFFFF)

CHECK_INPUT = b"123456789"
CHECKS = ((CRC8_MAXIM, 0xA1), (CRC16_USB, 0xB4C8), (CRC32_ETHERNET, 0xCBF43926))  # the catalogue's check values

# Codewords the catalogue lists: the message, then its CRC, low byte first.
CODEWORDS = (
    (CRC8_MAXIM, bytes.fromhex("242BC5FB00000040")),
    (CRC8_MAXIM, bytes.fromhex("102BC5FB000000A0")),
    (CRC16_USB, bytes.fromhex("8006000100004000DD94")),
    (CRC16_USB, bytes.fromhex("1BED534B3E6D7F7CD7CC")),
    (CRC16_USB, bytes.fromhex("00010203EF7A")),
    (CRC16_USB, bytes.fromhex("234567890E1C")),
    (CRC32_ETHERNET, bytes.fromhex("000000001CDF4421")),
    (CRC32_ETHERNET, bytes.fromhex("0FAA005587B2C9B6")),
)


def crc(params, data):
    width, poly, reg, xorout = params
    for bit in bits_lsb(data):
        feedback = reg >> width - 1 ^ bit
        reg = reg << 1 & (1 << width) - 1
        if feedback:
            reg ^= poly
    return int(format(reg, "0%db" % width)[::-1], 2) ^ xorout


def with_crc(params, data):
    return data + crc(params, data).to_bytes(params[0] // 8, "little")
