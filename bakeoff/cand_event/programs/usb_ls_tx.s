; Low-speed USB transmit: P0 = D+, P1 = D-. Host message: [n][PID][n-1 data bytes].
; Sends SYNC, PID, data, CRC-16 of the data, then SE0 for two bit times and J for one.
; The helpers do the bit work: timer t0 is the 40-cycle bit clock, the run-length unit stuffs a 0 after
; six 1s, the coder is NRZI, and the CRC unit is fed each data byte as it is queued.
; The program does the framing: what goes in, in which order, and how the packet ends.
.def BIT = 40

        cfg tx nbits=8 lsb code=nrzi stuff   ; the line starts at 0, which is J: D+ low, D- high
        #6 -> rl.n
        cfg pins p0=tx p1=~tx       ; D+ is the coder's line, D- its complement, while the shifter is busy
        cfg crc reflect xorout
        #0xA001 -> crc.poly.lo      ; 0x8005 reflected
        cfg tmr t0.auto
        #BIT -> t0
pkt:    fifo0 -> r1                 ; n
        #0xFFFF -> crc.lo
        #0x80 -> tx                 ; SYNC: seven 0s and a 1, LSB first, is KJKJKJKK
        fifo0 -> tx                 ; PID. It is not part of the CRC. Waits until SYNC is on the line.
        pin p0=0, p1=0              ; where the pins go when the shifter runs dry: SE0
        sub r1, #1
        br z, crc
data:   fifo0 -> tx, crc            ; one byte to the shifter and to the CRC unit
        djnz r1, data
crc:    crc.lo -> tx                ; low byte first; waits for the CRC unit to finish
        crc.lo.hi -> tx
        when tx.done: #2*BIT -> t1  ; the last bit ended this cycle and SE0 began
        when t1: pin p0=0, p1=1     ; J, driven
        #BIT -> t1
        when t1: pin p0=z, p1=z     ; let go
        goto pkt
