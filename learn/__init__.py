# SPDX-FileCopyrightText: © 2026 Mike Kovetsky
# SPDX-License-Identifier: Apache-2.0
"""Learn it, become it.

learn/host      runs on the host: the learner, the two bus adapters and the table generation. It imports nothing
                outside itself and reaches a device only through a link to the chip's host pins.
learn/programs  the two query engines that the chip runs while it questions a device.
learn/lab       the bench round it: hidden devices, the board, the judge and the baselines. Never imported by host.
"""
