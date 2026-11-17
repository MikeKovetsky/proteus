; SPI controller, mode 0, 5 MHz: P0 = SCK, P1 = MOSI, P2 = MISO, P3 = CS. Host message: [n][n bytes].
; The transmit shifter runs "split": two timer ticks per bit. P0 is its bit clock (low in the first half,
; high in the second), so MOSI changes on the falling edge and the rising edge sits mid-bit.
; MISO is sampled at the end of each bit, the latest moment the target's answer is still on the pin,
; which is what absorbs the three cycles of pad latency.
.def HALF = 6                       ; SCK period is 12 cycles

        pin p3=1, p0=0              ; CS high; SCK idles low (mode 0)
        cfg tx nbits=8 msb split
        cfg rx nbits=8 msb clock=end
        cfg smp data=p2             ; the pin the receive shifter samples
        cfg pins p0=clk p1=tx
        cfg tmr t0.auto
        #HALF -> t0
msg:    fifo0 -> r1                 ; n
        tst r1, #0xFF
        br z, msg                   ; n = 0: nothing to send
        pin p3=0                    ; select
        fifo0 -> tx                 ; first byte; it starts on the next tick
        sub r1, #1
        br z, last
more:   fifo0 -> tx                 ; the next byte waits in the holding register: no gap between bytes
        rx -> fifo1                 ; the byte that was clocked in meanwhile
        djnz r1, more
last:   rx -> fifo1
        when tx.done: pin p3=1      ; deselect
        goto msg
