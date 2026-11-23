// SPDX-FileCopyrightText: © 2026 Mike Kovetsky
// SPDX-License-Identifier: Apache-2.0
//
// Two whole chips on one set of pins, for learn-and-become's replacement test:
//   c  the original controller, a shipped program
//   x  the chip that becomes the device (held in reset, so released, while the device itself is there)
// and `far`, the device's own drivers or a tester's. Pins in `od` take RISE ns to read high once let go.
`default_nettype none
`timescale 1ns / 1ps

module tb_pair;
  parameter RISE = 300;

  reg c_clk, c_rst_n, x_clk, x_rst_n;
  reg [7:0] c_ui, x_ui, far_oe, far_out, pull, od;
  wire [7:0] c_uo, c_out, c_oe, x_uo, x_out, x_oe;
  wire [7:0] wired, seen;

  genvar i;
  generate
    for (i = 0; i < 8; i = i + 1) begin : pin
      assign wired[i] = c_oe[i] ? c_out[i] : 1'bz;
      assign wired[i] = x_oe[i] ? x_out[i] : 1'bz;
      assign wired[i] = far_oe[i] ? far_out[i] : 1'bz;
      assign (weak1, weak0) wired[i] = pull[i];
      wire slow;
      assign #(RISE, 0) slow = wired[i];
      assign seen[i] = od[i] ? slow : wired[i];
    end
  endgenerate

  tt_um_mikekovetsky_protoemu c (
      .ui_in(c_ui), .uo_out(c_uo), .uio_in(seen), .uio_out(c_out), .uio_oe(c_oe),
      .ena(1'b1), .clk(c_clk), .rst_n(c_rst_n)
  );

  tt_um_mikekovetsky_protoemu x (
      .ui_in(x_ui), .uo_out(x_uo), .uio_in(seen), .uio_out(x_out), .uio_oe(x_oe),
      .ena(1'b1), .clk(x_clk), .rst_n(x_rst_n)
  );
endmodule

`default_nettype wire
