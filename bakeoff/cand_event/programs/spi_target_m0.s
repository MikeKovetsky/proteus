; SPI target, mode 0: P0 = SCK (in), P1 = MOSI (in), P2 = MISO (out), P3 = CS (in).
; Receive: the sampler ticks two cycles after every rising SCK edge and shifts MOSI in by itself.
; Send: the program gives the transmit shifter one tick per falling SCK edge. P2 follows the shifter
; while it holds bits and is released otherwise, so MISO is never driven outside a transfer.
        cfg smp clk=p0 data=p1 edge=rise mode=gate
        #2 -> smp.phase
        cfg rx nbits=8 msb clock=smp
        cfg tx nbits=8 msb ext      ; ext: clocked by `act tx.tick`, not by a timer
        cfg pins p2=tx

start:  fifo0 -> tx                 ; the first byte waits in the holding register
        when !p3: act tx.tick, smp.arm   ; CS fell: bit 7 goes out, the sampler listens
        trap p3 -> done             ; CS rising ends the transfer wherever the loop is
byte:   br !in0, bit
        fifo0 -> tx                 ; queue the next byte behind the one being shifted
bit:    when p0                     ; SCK high: the sampler is taking MOSI
        when !p0: act tx.tick       ; SCK low again: next bit on MISO
        br !rx.full, bit
        rx -> fifo1
        goto byte

done:   act tx.flush, smp.stop, rx.clear
        goto start
