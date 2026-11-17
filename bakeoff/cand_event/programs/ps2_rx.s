; PS/2 receive: P0 = CLK, P1 = DATA, both driven by the device. Good bytes go to channel 1; a frame with
; wrong parity puts 0xFF on channel 2.
; The sampler ticks four cycles after every falling clock edge, so a whole 11-bit frame (start, eight data
; bits, odd parity, stop) lands in the receive shifter as one word.
; Parity is the CRC unit with the polynomial x + 1: its low bit is the XOR of every bit fed to it.
        cfg smp clk=p0 data=p1 edge=fall mode=gate
        #4 -> smp.phase
        cfg rx nbits=11 lsb clock=smp
        cfg crc reflect
        #1 -> crc.poly.lo
        act smp.arm
frame:  #0 -> crc.lo
        rx -> r0, crc               ; waits for a frame; its low byte goes through the parity unit
        r0.hi -> crc                ; and so do bits 8 to 10
        shr r0                      ; drop the start bit: the data byte is now the low byte
        crc.lo -> r1
        tst r1, #1                  ; start (0) + data + parity + stop (1) has an even number of 1s when
        br !z, bad                  ; the parity is right
        r0 -> fifo1
        goto frame
bad:    #0xFF -> fifo2
        goto frame
