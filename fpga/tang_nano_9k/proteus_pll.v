/*
 * Copyright (c) 2026 Mike Kovetsky
 * SPDX-License-Identifier: Apache-2.0
 *
 * Exact 27.000 MHz -> 60.000 MHz PLL for Tang Nano 9K (GW1NR-9 C6/I5),
 * generated with `gowin_pll -d "GW1NR-9 C6/I5" -i 27 -o 60`.
 *
 *   F_PFD = 27.0 MHz / (8 + 1) = 3.0 MHz   (spec: 3..400 MHz)
 *   F_OUT = 3.0 MHz * (19 + 1) = 60.0 MHz  (exact match to Tiny Tapeout ASIC target!)
 *   F_VCO = 60.0 MHz * 8       = 480.0 MHz (spec: 400..1200 MHz)
 */

`default_nettype none

module proteus_pll (
    input  wire clock_in,
    output wire clock_out,
    output wire locked
);

`ifdef SIM
  assign clock_out = clock_in;
  assign locked    = 1'b1;
`else
  rPLL #(
      .FCLKIN("27.0"),
      .IDIV_SEL(8),
      .FBDIV_SEL(19),
      .ODIV_SEL(8)
  ) pll (
      .CLKOUT   (clock_out),
      .LOCK     (locked),
      .CLKOUTP  (),
      .CLKOUTD  (),
      .CLKOUTD3 (),
      .RESET    (1'b0),
      .RESET_P  (1'b0),
      .CLKFB    (1'b0),
      .FBDSEL   (6'b0),
      .IDSEL    (6'b0),
      .ODSEL    (6'b0),
      .PSDA     (4'b0),
      .DUTYDA   (4'b0),
      .FDLY     (4'b0),
      .CLKIN    (clock_in)
  );
`endif

endmodule

`default_nettype wire
