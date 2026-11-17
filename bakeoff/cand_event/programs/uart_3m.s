; UART at 3 Mbaud, both directions at once, on one lane: P0 = TX, P1 = RX.
; The main loop sends. A received byte interrupts it through the trap and resumes it.
.def BIT = 20

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
        act smp.arm                 ; ready for the next start edge
        trap rx.full -> got
        resume                      ; back to the instruction that was interrupted
