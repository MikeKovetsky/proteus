; UART receive: 8N1 at 115200 baud on P1. Each byte goes to channel 1.
; The sampler waits for the start edge, then ticks in the middle of every data bit.
.def BIT = 521

        cfg smp clk=p1 data=p1 edge=fall mode=once
        #BIT -> smp.period
        #BIT*3/2 -> smp.phase       ; from the start edge to the middle of data bit 0
        cfg rx nbits=8 lsb clock=smp
next:   act smp.arm                 ; listen for a start edge (the bit clock stops until it comes)
        rx -> fifo1                 ; waits for eight bits
        goto next
