; I2C controller, fast mode: P0 = SCL, P1 = SDA, both open drain.
; Host message: [addr7][nw][nw bytes][nr]. Bytes read go to channel 1, then a status byte to channel 2
; (0 = done, 1 = no acknowledge).
; One subroutine, xfer, clocks nine bits. The word in r1 is what the controller puts on SDA (a 1 releases
; the line) and the word that comes back is what was on SDA, so the same nine clocks write a byte and read
; its ACK, or read a byte and send the ACK.
.def LOW = 94                       ; SCL low for LOW + 7 cycles
.def HIGH = 48                      ; SCL high for HIGH + 5 cycles: a period of 154
.def HOLD = 40                      ; set-up and hold around START and STOP

        cfg od p0 p1                ; open drain: these pins only ever pull low
        pin p0=1, p1=1
        cfg tx nbits=9 msb ext      ; shifted by the program, one bit per SCL clock
        cfg rx nbits=9 msb clock=manual
        cfg smp data=p1
        cfg pins p1=tx              ; SDA follows the shifter during a byte, its own register otherwise

msg:    fifo0 -> r0                 ; the address
        shl r0                      ; address + write
        pin p1=0                    ; START: SDA falls while SCL is high
        #HOLD -> t1
        when t1: pin p0=0
        r0 -> r1
        shl r1
        or r1, #1                   ; eight bits, then release SDA for the ACK
        call xfer
        br !z, nack
        fifo0 -> r3                 ; nw
        tst r3, #0xFF
        br z, again
wr:     fifo0 -> r1
        shl r1
        or r1, #1
        call xfer
        br !z, nackw
        djnz r3, wr

again:  #LOW -> t1                  ; repeated START: release SCL, then SDA falls
        when t1: pin p0=1
        when p0: #HOLD -> t1
        when t1: pin p1=0
        #HOLD -> t1
        when t1: pin p0=0
        r0 -> r1
        or r1, #1                   ; address + read
        shl r1
        or r1, #1
        call xfer
        br !z, nackr
        fifo0 -> r3                 ; nr
rd:     #0x1FE -> r1                ; release SDA for eight bits, then pull it low: ACK
        cmp r3, #1
        br !z, rd2
        #0x1FF -> r1                ; the last byte is not acknowledged
rd2:    call xfer
        shr r1                      ; drop the ACK slot
        r1 -> fifo1
        djnz r3, rd
        #0 -> r3                    ; status: done

stop:   pin p1=0                    ; STOP: SDA low while SCL is low,
        #LOW -> t1
        when t1: pin p0=1
        when p0: #HOLD -> t1
        when t1: pin p1=1           ; then rising while SCL is high
        r3 -> fifo2
        #LOW -> t1                  ; bus free time
        when t1
        goto msg

nack:   fifo0 -> r3                 ; nobody answered: take the rest of the message off the FIFO
        tst r3, #0xFF
        br z, nackr                 ; nw = 0: only nr is left
skip:   fifo0 -> r1
nackw:  djnz r3, skip
nackr:  fifo0 -> r1                 ; nr
        #1 -> r3                    ; status: no acknowledge
        goto stop

; xfer: clock nine bits, MSB first. In: r1 = word to send. Out: r1 = word read, z = the ninth bit was 0.
xfer:   r1 -> tx
        #9 -> r2
xbit:   act tx.tick                 ; next bit on SDA; SCL is low
        #LOW -> t1
        pin p1=1                    ; behind the shifter, SDA's own register is released
        when t1: pin p0=1           ; release SCL
        when p0: act rx.shift       ; wait until SCL really is high (a target may hold it), then sample
        #HIGH -> t1
        when t1: pin p0=0
        djnz r2, xbit
        act tx.flush                ; SDA back to its register
        rx -> r1
        tst r1, #1
        ret
