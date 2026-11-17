/*
 * Copyright (c) 2026 Mike Kovetsky
 * SPDX-License-Identifier: Apache-2.0
 *
 * FLOW TEST, NOT THE PROTOEMU DESIGN. The real RTL replaces this file.
 *
 * It proves that Tiny Tapeout's flow accepts, places, powers, checks and simulates the two IHP 512 x 32 SRAM
 * macros the table architecture needs, on a design about the size of the real one:
 *
 *   1. The two macros form one 512 x 64 table: same address and controls, word A (bits 31:0) in sram_a and
 *      word B (bits 63:32) in sram_b. src/config.json places and powers them by these instance names.
 *   2. After reset a loader writes all 512 entries with a 64-bit LFSR sequence, reads them back, and reports
 *      pass or fail on uo_out. With ui_in[3:0] = 15 while it writes, it flips one bit in each of 16 entries
 *      (5, 37, ..., 485; bit 1, 5, ..., 61), so the test can see the fail path and the mismatch count work.
 *   3. Then it walks the table for 1024 clocks, one entry per clock, through the same loop the table
 *      architecture has: SRAM output, a 16:1 condition select, an OR, a 2:1 mux, SRAM address. The flow's
 *      timing report on that loop is the 60 MHz answer for the real design.
 *   4. Every word read is folded into a signature and into 16 lanes of 64 flops (flowtest_lanes.v), so
 *      routing and the clock tree see about 1,200 flops. uio_out shows any byte of the signature or of the
 *      last lane, so the test can check all of it.
 *
 * Pins:
 *   ui_in[3:0]  byte shown on uio_out: 0-7 the signature, 8-15 the last lane (byte 0 is bits 7:0);
 *               15 during the writes also plants the 16 faulty entries
 *   ui_in[7:4]  four condition inputs of the walk; hold them from reset until the walk is done
 *   uo_out[0]   self-test done          uo_out[1] self-test passed     uo_out[2] self-test failed
 *   uo_out[3]   walk done               uo_out[7:4] mismatching words, saturating at 15
 *   uio_out     the selected byte; uio_oe is all ones
 *
 * Every flop resets through rst_n (asynchronous, active low).
 */

`default_nettype none

/* verilator lint_off DECLFILENAME */  // the file name says this is the flow test, not the top's name
module tt_um_mikekovetsky_protoemu (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  // The pattern: a maximal-length 64-bit Galois LFSR (taps 64, 63, 61, 60), one state per address.
  localparam [63:0] SEED = 64'h0123_4567_89AB_CDEF;
  localparam [63:0] TAPS = 64'hD800_0000_0000_0000;

  localparam [2:0] ST_INIT  = 3'd0;  // one idle clock after reset
  localparam [2:0] ST_WRITE = 3'd1;  // write entries 0..511
  localparam [2:0] ST_READ  = 3'd2;  // read entries 0..511, compared one clock later
  localparam [2:0] ST_WALK  = 3'd3;  // 1024 table-driven reads
  localparam [2:0] ST_IDLE  = 3'd4;  // done; the table is idle

  localparam [10:0] WALK_LAST = 11'd1023;  // 1024 walk reads
  localparam integer LANES = 16;

  // ---- two-flop input synchroniser
  reg [7:0] ui_meta, ui_sync;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      ui_meta <= 8'd0;
      ui_sync <= 8'd0;
    end else begin
      ui_meta <= ui_in;
      ui_sync <= ui_meta;
    end
  end

  // ---- state
  reg [2:0] state;
  reg [8:0] cnt;  // loader address
  reg [63:0] lfsr;  // pattern: written in ST_WRITE, expected value in the compare clocks
  reg rd_valid;  // a read was issued in the last clock, so dout holds data
  reg rd_check;  // ... and it was a self-test read, so compare it
  reg st_done, st_fail;
  reg [3:0] nmis;
  reg [10:0] wcnt;
  reg walk_done;
  reg [63:0] sig;
  reg [7:0] rd_byte;

  wire [63:0] dout;  // {word B, word A}
  wire [63:0] lfsr_next = {1'b0, lfsr[63:1]} ^ (lfsr[0] ? TAPS : 64'd0);

  // ---- the table loop: entry -> condition select -> OR -> mux -> address, within one clock
  wire [15:0] cond = {ui_sync[7:4], wcnt[9:0], sig[1:0]};
  wire [8:0] base = dout[8:0];
  wire c_a = cond[dout[12:9]];
  wire c_b = cond[dout[16:13]];
  wire [8:0] walk_addr = {base[8:2], base[1] | c_b, base[0] | c_a};

  wire in_write = (state == ST_WRITE);
  wire in_read = (state == ST_READ);
  wire in_walk = (state == ST_WALK);

  wire t_men = in_write | in_read | in_walk;
  wire t_wen = in_write;
  wire t_ren = in_read | in_walk;
  wire [8:0] t_addr = in_walk ? walk_addr : cnt;

  // Planted faults: entry 32j+5 gets bit 4j+1 flipped, j = 0..15, when ui_in[3:0] is 15.
  wire inject = (ui_sync[3:0] == 4'hF) && (cnt[4:0] == 5'd5);
  wire [63:0] t_din = lfsr ^ (inject ? (64'd1 << {cnt[8:5], 2'b01}) : 64'd0);

  // ---- the 512 x 64 table: two IHP macros side by side. A_DLY must be 1; BIST is off.
  RM_IHPSG13_1P_512x32_c2_bm_bist sram_a (
      .A_CLK      (clk),
      .A_MEN      (t_men),
      .A_WEN      (t_wen),
      .A_REN      (t_ren),
      .A_ADDR     (t_addr),
      .A_DIN      (t_din[31:0]),
      .A_DLY      (1'b1),
      .A_DOUT     (dout[31:0]),
      .A_BM       ({32{1'b1}}),
      .A_BIST_CLK (1'b0),
      .A_BIST_EN  (1'b0),
      .A_BIST_MEN (1'b0),
      .A_BIST_WEN (1'b0),
      .A_BIST_REN (1'b0),
      .A_BIST_ADDR(9'd0),
      .A_BIST_DIN (32'd0),
      .A_BIST_BM  (32'd0)
  );

  RM_IHPSG13_1P_512x32_c2_bm_bist sram_b (
      .A_CLK      (clk),
      .A_MEN      (t_men),
      .A_WEN      (t_wen),
      .A_REN      (t_ren),
      .A_ADDR     (t_addr),
      .A_DIN      (t_din[63:32]),
      .A_DLY      (1'b1),
      .A_DOUT     (dout[63:32]),
      .A_BM       ({32{1'b1}}),
      .A_BIST_CLK (1'b0),
      .A_BIST_EN  (1'b0),
      .A_BIST_MEN (1'b0),
      .A_BIST_WEN (1'b0),
      .A_BIST_REN (1'b0),
      .A_BIST_ADDR(9'd0),
      .A_BIST_DIN (32'd0),
      .A_BIST_BM  (32'd0)
  );

  // ---- loader, self-test and walk
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state     <= ST_INIT;
      cnt       <= 9'd0;
      lfsr      <= SEED;
      rd_valid  <= 1'b0;
      rd_check  <= 1'b0;
      st_done   <= 1'b0;
      st_fail   <= 1'b0;
      nmis      <= 4'd0;
      wcnt      <= 11'd0;
      walk_done <= 1'b0;
      sig       <= 64'd0;
    end else begin
      rd_valid <= t_ren;
      rd_check <= in_read;

      case (state)
        ST_INIT: state <= ST_WRITE;
        ST_WRITE: begin
          cnt <= cnt + 9'd1;
          if (cnt == 9'd511) begin
            lfsr  <= SEED;  // the read-back expects the same sequence
            state <= ST_READ;
          end else begin
            lfsr <= lfsr_next;
          end
        end
        ST_READ: begin
          cnt <= cnt + 9'd1;
          if (cnt == 9'd511) state <= ST_WALK;
        end
        ST_WALK: begin
          wcnt <= wcnt + 11'd1;
          if (wcnt == WALK_LAST) state <= ST_IDLE;
        end
        ST_IDLE: walk_done <= 1'b1;
        default: state <= ST_IDLE;
      endcase

      // The compare clocks follow the read clocks; they never overlap ST_WRITE.
      if (rd_check) begin
        lfsr <= lfsr_next;
        if (dout != lfsr) begin
          st_fail <= 1'b1;
          if (nmis != 4'hF) nmis <= nmis + 4'd1;
        end
        if (in_walk) st_done <= 1'b1;  // the last compare falls in the first walk clock
      end

      // The signature takes every word the walk reads.
      if (rd_valid && !rd_check) sig <= {sig[62:0], sig[63]} ^ dout;
    end
  end

  // ---- filler: every word read goes through 16 chained lanes of 64 flops
  wire [63:0] lane_last;
  flowtest_lanes #(
      .N(LANES)
  ) lanes (
      .clk  (clk),
      .rst_n(rst_n),
      .en   (rd_valid),
      .din  (dout),
      .last (lane_last)
  );

  // ---- read-out
  wire [127:0] readout = {lane_last, sig};
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) rd_byte <= 8'd0;
    else rd_byte <= readout[{ui_sync[3:0], 3'b000}+:8];
  end

  assign uo_out  = {nmis, walk_done, st_fail, st_done & ~st_fail, st_done};
  assign uio_out = rd_byte;
  assign uio_oe  = 8'hFF;

  wire _unused = &{ena, uio_in, 1'b0};

endmodule

`default_nettype wire
