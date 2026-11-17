/*
 * Copyright (c) 2026 Mike Kovetsky
 * SPDX-License-Identifier: Apache-2.0
 *
 * FLOW TEST, NOT THE PROTOEMU DESIGN. Filler logic for flowtest_top.v.
 *
 * N chained lanes of 64 flops. On every clock with en high, lane i becomes itself rotated left by 2i+1, XOR its
 * input. Lane 0's input is din, all 64 bits. Lane i's input is lane i-1 (its value before the clock), even bits
 * only: that keeps the logic near the real design's estimate of 2,600 cells, and the odd rotations still carry
 * every bit of lane i-1 into lane i within two clocks. The last lane therefore depends on every flop of every
 * lane, so synthesis keeps them all and the test checks them all through the last lane.
 */

`default_nettype none

module flowtest_lanes #(
    parameter integer N = 16
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        en,
    input  wire [63:0] din,
    output wire [63:0] last
);

  localparam [63:0] EVEN = 64'h5555_5555_5555_5555;

  wire [64*(N+1)-1:0] chain;  // chain[64*i +: 64] is lane i's input; the top 64 bits are the last lane
  assign chain[63:0] = din;

  genvar i;
  generate
    for (i = 0; i < N; i = i + 1) begin : g_lane
      localparam integer R = 2 * i + 1;
      localparam [63:0] MASK = (i == 0) ? {64{1'b1}} : EVEN;
      reg [63:0] q;
      always @(posedge clk or negedge rst_n) begin
        if (!rst_n) q <= 64'd0;
        else if (en) q <= {q[63-R:0], q[63:64-R]} ^ (chain[64*i+:64] & MASK);
      end
      assign chain[64*(i+1)+:64] = q;
    end
  endgenerate

  assign last = chain[64*N+:64];

endmodule

`default_nettype wire
