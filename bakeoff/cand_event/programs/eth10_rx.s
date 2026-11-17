; 10 Mbit Ethernet receive on P2. Per frame: every byte after the start delimiter goes to channel 1, then
; channel 2 gets [0 good / 1 bad FCS][length low][length high].
; A Manchester bit always has an edge in its middle. The sampler (mode=gate) takes an edge, ignores the
; line for 4 cycles, samples it, and only then listens again: the sample lands in the first half of the next
; bit, whose level is that bit inverted, and the edge between two bits is never mistaken for a mid-bit one.
; The matcher finds the delimiter in the bit stream, and timer t1, restarted by every edge, is the carrier
; watchdog. The program counts bytes and checks the CRC-32 residue.
        cfg smp clk=p2 data=p2 edge=any mode=gate invert
        #4 -> smp.phase
        cfg rx nbits=8 lsb clock=smp
        cfg crc reflect
        #0x8320 -> crc.poly.lo
        #0xEDB8 -> crc.poly.hi
        #0xD5 -> match.val          ; the last eight bits are the start delimiter
        #0xFF -> match.mask
        cfg tmr t1.retrig=smp.edge  ; t1 starts again at every edge: it expires when the line goes quiet
frame:  when !p2                    ; the line is idle
        act smp.arm, rx.clear
        #0xFFFF -> crc.lo
        #0xFFFF -> crc.hi
        #0 -> r1                    ; byte count
        when match: act rx.clear    ; the delimiter: bytes are counted from here
        #12 -> t1
        trap t1 -> eof
byte:   rx -> fifo1, crc
        add r1, #1
        goto byte

eof:    act smp.stop
        crc.lo -> r0                ; the CRC-32 register holds 0xDEBB20E3 after a good frame and its FCS
        cmp r0, #0x20E3
        br !z, bad
        crc.hi -> r0
        cmp r0, #0xDEBB
        br !z, bad
        #0 -> fifo2
        goto len
bad:    #1 -> fifo2
len:    r1 -> fifo2
        r1.hi -> fifo2
        goto frame
