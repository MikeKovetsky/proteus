# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""What each protocol says is correct: decoders, far ends and timing limits for the pin tests.

They are written from each protocol's own definition (named at the top of each module) and import nothing from
the bake-off harness, the golden model or the compiler. They are plain Python without cocotb. Times are
nanoseconds and pins are waveforms (wave.Wave).
"""
