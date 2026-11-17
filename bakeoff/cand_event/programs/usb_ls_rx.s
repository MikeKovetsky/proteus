; Low-speed USB receive: P0 = D+, P1 = D-. The chip only listens.
; Per packet: every byte after SYNC goes to channel 1, then channel 2 gets [0 good / 1 bad CRC][byte count].
; The sampler recovers the bit clock: every edge on D+ re-centres it, so it samples mid-bit at any rate
; near 40 cycles. NRZI decoding and removing the stuffed bits are helper settings. The pattern matcher
; watches the pins for SE0, and the program's trap turns that into "end of packet".
.def BIT = 40

        cfg smp clk=p0 data=p0 edge=any mode=free nrzi
        #BIT -> smp.period
        #BIT/2 -> smp.phase
        cfg rx nbits=8 lsb clock=smp destuff
        #6 -> rl.n
        cfg crc reflect
        #0xA001 -> crc.poly.lo
        cfg match src=pins
        #3 -> match.mask            ; D+ and D- ...
        #0 -> match.val             ; ... both low
pkt:    act smp.arm, rx.clear, rl.clear   ; the bit clock starts at the first edge
        #0xFFFF -> crc.lo
        #0 -> r1                    ; byte count
        rx -> r0                    ; SYNC
        trap match -> eop
        rx -> fifo1                 ; PID: not part of the CRC
        add r1, #1
byte:   rx -> fifo1, crc
        add r1, #1
        goto byte

eop:    act smp.stop
        crc.lo -> r0
        cmp r0, #0xB001             ; what the register holds after good data and its CRC
        br z, good
        #1 -> fifo2
        goto count
good:   #0 -> fifo2
count:  r1 -> fifo2
        when !match                 ; the end of SE0
        #2*BIT -> t1                ; and of the J after it
        when t1
        goto pkt
