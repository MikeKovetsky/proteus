; 1-Wire controller at standard speed on P0 (open drain): reset, write 0x33 (Read ROM), read 64 bits.
; The 8 bytes go to channel 1. Channel 2 gets 0 (a device answered) then 0 (CRC good) or 1 (CRC bad);
; with no device it gets a single 1.
; All slot timing is the program's, on timer t1. The receive shifter packs the bits into bytes and the CRC
; unit checks them (x^8 + x^5 + x^4 + 1, reflected 0x8C: the register is 0 after data and CRC).
.def US = 60                        ; cycles per microsecond

        cfg od p0
        pin p0=1
        cfg smp data=p0
        cfg rx nbits=8 lsb clock=manual
        cfg crc reflect
        #0x8C -> crc.poly.lo
        #0 -> crc.lo

        pin p0=0                    ; reset: low for 500 us
        #500*US -> t1
        when t1: pin p0=1
        #70*US -> t1                ; a device pulls low from 30 to 150 us after the release
        when t1
        br p0, absent
        #410*US -> t1               ; let the presence pulse end
        when t1

        #0x33 -> r0
        #8 -> r2
wbit:   pin p0=0                    ; a write slot, LSB first
        shr r0                      ; carry = the bit
        br c, w1
        #70*US -> t1                ; a 0 is low for 70 us
        when t1: pin p0=1
        #10*US -> t1
        when t1
        goto wnext
w1:     #5*US -> t1                 ; a 1 is low for 5 us
        when t1: pin p0=1
        #75*US -> t1
        when t1
wnext:  djnz r2, wbit

        #64 -> r2
rbit:   pin p0=0                    ; a read slot: low for 3 us,
        #3*US -> t1
        when t1: pin p0=1
        #10*US -> t1                ; sample 13 us after the slot began
        when t1: act rx.shift
        #60*US -> t1
        when t1
        br !rx.full, rnext
        rx -> fifo1, crc            ; every eighth slot: a byte for the host and for the CRC
rnext:  djnz r2, rbit

        #0 -> fifo2                 ; present
        crc.lo -> r0
        tst r0, #0xFF
        br z, good
        #1 -> fifo2
        halt
good:   #0 -> fifo2
        halt
absent: #1 -> fifo2
        halt
