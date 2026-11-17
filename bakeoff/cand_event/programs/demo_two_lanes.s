; Two protocols at once, one per lane. Not a bake-off scenario: selfcheck.py runs it.
; Lane 0 is uart_3m.s unchanged: a full-duplex UART at 3 Mbaud on P0 and P1, channels 0 and 1.
; Lane 1 is spi_ctrl_m0.s moved to P4..P7 and to the spare channels: [n][n bytes] comes from channel 3
; and the bytes read go to channel 2.
.def BIT = 20
.def HALF = 6

.lane 0
        pin p0=1
        cfg tx nbits=10 lsb
        cfg pins p0=tx
        cfg tmr t0.auto
        #BIT -> t0
        cfg smp clk=p1 data=p1 edge=fall mode=once
        #BIT -> smp.period
        #BIT*3/2 -> smp.phase
        cfg rx nbits=8 lsb clock=smp
        act smp.arm
        trap rx.full -> got
send:   fifo0 -> r0
        shl r0
        or r0, #0x200
        r0 -> tx
        goto send
got:    rx -> fifo1
        act smp.arm
        trap rx.full -> got
        resume

.lane 1
        pin p7=1, p4=0              ; CS high, SCK low
        cfg tx nbits=8 msb split
        cfg rx nbits=8 msb clock=end
        cfg smp data=p6
        cfg pins p4=clk p5=tx
        cfg tmr t0.auto
        #HALF -> t0
msg:    fifo3 -> r1
        pin p7=0
        fifo3 -> tx
        sub r1, #1
        br z, last
more:   fifo3 -> tx
        rx -> fifo2
        djnz r1, more
last:   rx -> fifo2
        when tx.done: pin p7=1
        goto msg
