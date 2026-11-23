/*
 * Copyright (c) 2026 Mike Kovetsky
 * SPDX-License-Identifier: Apache-2.0
 *
 * proteus_table: the 512 x 64 state table, as two IHP 512x32 single-port SRAM macros side by side.
 *
 * Word A (bits 31:0) is in the first macro and word B (bits 63:32) in the second, at the same address, so
 * both are read together. The macros are synchronous: the address is latched on a rising edge and the
 * entry is on `dout` after it, until the next read. `bm` is a bit-write mask (1 writes the bit), which
 * lets the loader write one byte lane at a time. The BIST port is unused and tied off; A_DLY must be 1.
 */

`default_nettype none

module proteus_table (
    input  wire        clk,
    input  wire        men,
    input  wire        wen,
    input  wire        ren,
    input  wire [8:0]  addr,
    input  wire [63:0] din,
    input  wire [63:0] bm,
    output wire [63:0] dout
);

  RM_IHPSG13_1P_512x32_c2_bm_bist ram_a (
      .A_CLK      (clk),
      .A_MEN      (men),
      .A_WEN      (wen),
      .A_REN      (ren),
      .A_ADDR     (addr),
      .A_DIN      (din[31:0]),
      .A_DLY      (1'b1),
      .A_DOUT     (dout[31:0]),
      .A_BM       (bm[31:0]),
      .A_BIST_CLK (1'b0),
      .A_BIST_EN  (1'b0),
      .A_BIST_MEN (1'b0),
      .A_BIST_WEN (1'b0),
      .A_BIST_REN (1'b0),
      .A_BIST_ADDR(9'd0),
      .A_BIST_DIN (32'd0),
      .A_BIST_BM  (32'd0)
  );

  RM_IHPSG13_1P_512x32_c2_bm_bist ram_b (
      .A_CLK      (clk),
      .A_MEN      (men),
      .A_WEN      (wen),
      .A_REN      (ren),
      .A_ADDR     (addr),
      .A_DIN      (din[63:32]),
      .A_DLY      (1'b1),
      .A_DOUT     (dout[63:32]),
      .A_BM       (bm[63:32]),
      .A_BIST_CLK (1'b0),
      .A_BIST_EN  (1'b0),
      .A_BIST_MEN (1'b0),
      .A_BIST_WEN (1'b0),
      .A_BIST_REN (1'b0),
      .A_BIST_ADDR(9'd0),
      .A_BIST_DIN (32'd0),
      .A_BIST_BM  (32'd0)
  );

endmodule
