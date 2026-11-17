/*
 * Copyright (c) 2026 Mike Kovetsky
 * SPDX-License-Identifier: Apache-2.0
 *
 * protoemu_fifo: a 4-byte FIFO between the host and the machines.
 *
 * It does what it is told. The caller decides whether a push or a pop is allowed: a push only when there
 * is room after this clock's pop, a pop only when there is a byte after this clock's push. A push into a
 * full FIFO that pops in the same clock writes the slot being popped, which is right for a ring.
 */

`default_nettype none

module protoemu_fifo (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       push,
    input  wire [7:0] din,
    input  wire       pop,
    output reg  [2:0] cnt,
    output wire [7:0] head
);

  reg [31:0] mem;
  reg [1:0]  rd;
  reg [1:0]  wr;

  assign head = mem[rd*8+:8];

  integer k;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      mem <= 32'd0;
      rd  <= 2'd0;
      wr  <= 2'd0;
      cnt <= 3'd0;
    end else begin
      if (push) begin
        for (k = 0; k < 4; k = k + 1) begin
          if (wr == k[1:0]) mem[k*8+:8] <= din;
        end
        wr <= wr + 2'd1;
      end
      if (pop) rd <= rd + 2'd1;
      cnt <= cnt + {2'b00, push} - {2'b00, pop};
    end
  end

endmodule
