; UART transmit: 8N1 at 115200 baud on P0. The host puts bytes in channel 0.
; A frame is one 10-bit word in the transmit shifter: start (0), eight data bits, stop (1).
.def BIT = 521                      ; 60 MHz / 115200

        pin p0=1                    ; the line idles high
        cfg tx nbits=10 lsb         ; code=nrz by default
        cfg pins p0=tx              ; P0 follows the shifter while it is sending
        cfg tmr t0.auto
        #BIT -> t0                  ; timer 0 is the bit clock
next:   fifo0 -> r0                 ; waits for a byte
        shl r0                      ; bit 0 becomes the start bit
        or r0, #0x200               ; bit 9 is the stop bit
        r0 -> tx                    ; waits for the holding register, so frames follow back to back
        goto next
