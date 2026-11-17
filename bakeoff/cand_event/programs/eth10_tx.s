; 10 Mbit Ethernet transmit: P0 = TX+, P1 = TX-. Host message: [len low][len high][len bytes].
; Sends the preamble, the start delimiter, the frame and its CRC-32, Manchester coded.
; Timer t0 ticks every 3 cycles: one half bit. The coder is "split" (two ticks per bit) and Manchester,
; so a 1 goes out low then high. The program only has to keep the holding register fed, once per byte.
.def HALF = 3

        pin p0=0, p1=0              ; idle: both low
        cfg tx nbits=8 lsb code=manch split
        cfg pins p0=tx p1=~tx
        cfg crc reflect xorout
        #0x8320 -> crc.poly.lo      ; 0x04C11DB7 reflected is 0xEDB88320
        #0xEDB8 -> crc.poly.hi
        cfg tmr t0.auto
        #HALF -> t0
frame:  fifo0 -> r1                 ; length, low byte
        fifo0 -> r1.hi              ; and high byte
        #0xFFFF -> crc.lo
        #0xFFFF -> crc.hi
        #7 -> r2
pre:    #0x55 -> tx
        djnz r2, pre
        #0xD5 -> tx
        pin p0=1, p1=0              ; where the pins go when the shifter runs dry: TX+ high
data:   fifo0 -> tx, crc
        djnz r1, data
        crc.lo -> tx                ; the FCS, low byte first
        crc.lo.hi -> tx
        crc.hi -> tx
        crc.hi.hi -> tx
        when tx.done: #18 -> t1     ; the last bit ended this cycle; hold TX+ high for 18 cycles
        when t1: pin p0=0, p1=0     ; idle
        #600 -> t1                  ; the gap between frames
        when t1
        goto frame
