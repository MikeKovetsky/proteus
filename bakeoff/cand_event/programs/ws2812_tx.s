; WS2812 transmit on P0. Host message: [n pixels][3n bytes], each byte MSB first.
; No helper shapes the pulses: the program does it with two timers. t0 marks the start of every bit
; (75 cycles); t1 times the high part (24 cycles for a 0, 48 for a 1).
.def BIT = 75

        pin p0=0
        cfg tmr t0.auto
msg:    fifo0 -> r3                 ; pixels
        r3 -> r2
        shl r3
        add r3, r2                  ; three bytes each
        when in0: #BIT -> t0        ; start the bit clock once the first byte is here
byte:   fifo0 -> r0.hi              ; into the top half of r0, so a shift left drops each bit into carry
        #8 -> r1
bit:    when t0: pin p0=1           ; cycle T: the bit starts
        shl r0                      ; T+2: carry = this bit
        br c, one                   ; T+4
        #18 -> t1                   ; T+6: t1 expires at T+24
        when t1: pin p0=0
        goto next
one:    #42 -> t1                   ; expires at T+48
        when t1: pin p0=0
next:   djnz r1, bit
        djnz r3, byte
        act t0.stop
        #3100 -> t1                 ; the latch time: stay low
        when t1
        goto msg
