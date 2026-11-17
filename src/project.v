/*
 * Copyright (c) 2026 Mike Kovetsky
 * SPDX-License-Identifier: Apache-2.0
 *
 * protoemu: a table-driven protocol emulator for Tiny Tapeout (IHP SG13CMOS5L, 8x4 tiles, 60 MHz).
 *
 * Pins (docs/info.md has the host's side in full):
 *   uio[7:0]    P0..P7, the protocol pins. In through a 2-flop synchroniser, out from the core's pad
 *               registers. A pin the chip reacts to at once changes 3 clocks after it was seen.
 *   ui_in[3:0]  host data in, one nibble at a time, low nibble first
 *   ui_in[5:4]  port: 0 = write channel 0, read channel 1; 1 = write channel 3, read channel 2;
 *               2 = the loader; 3 = nothing
 *   ui_in[6]    write strobe: every edge, rising or falling, takes one nibble from ui_in[3:0]
 *   ui_in[7]    read strobe: every edge moves uo_out[3:0] on by one nibble; the second pops the byte
 *   uo_out[3:0] host data out: the low, then the high nibble of the selected read channel's oldest byte
 *   uo_out[4]   the selected write channel has room (port 2: the loader takes bytes)
 *   uo_out[5]   the selected read channel has a byte
 *   uo_out[6]   running: the table is loaded and the machines step
 *   uo_out[7]   overflow: a byte was dropped, by a machine pushing into a full FIFO or by the host
 */

`default_nettype none

// The template names this file project.v; Tiny Tapeout names the module.
/* verilator lint_off DECLFILENAME */
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

  // ---- reset: asserted at once, released on a clock edge ----------------------------------------------

  // A reset synchroniser is the one place a flop is both data and an asynchronous reset.
  /* verilator lint_off SYNCASYNCNET */
  reg [1:0] rst_sync;
  /* verilator lint_on SYNCASYNCNET */
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) rst_sync <= 2'b00;
    else rst_sync <= {rst_sync[0], 1'b1};
  end
  wire rst_i = rst_sync[1];

  // ---- input synchronisers: protocol pins and host pins -----------------------------------------------

  reg [7:0] pin_s1, pin_s2;
  reg [7:0] ui_s1, ui_s2;
  reg       wr_d, rd_d;  // the strobes one clock ago, for edge detection
  always @(posedge clk or negedge rst_i) begin
    if (!rst_i) begin
      pin_s1 <= 8'd0;
      pin_s2 <= 8'd0;
      ui_s1  <= 8'd0;
      ui_s2  <= 8'd0;
      wr_d   <= 1'b0;
      rd_d   <= 1'b0;
    end else begin
      pin_s1 <= uio_in;
      pin_s2 <= pin_s1;
      ui_s1  <= ui_in;
      ui_s2  <= ui_s1;
      wr_d   <= ui_s2[6];
      rd_d   <= ui_s2[7];
    end
  end

  // ---- host link ---------------------------------------------------------------------------------------

  wire [3:0] h_nib   = ui_s2[3:0];
  wire [1:0] h_port  = ui_s2[5:4];
  wire       wr_edge = ui_s2[6] ^ wr_d;
  wire       rd_edge = ui_s2[7] ^ rd_d;

  reg        wr_hi;   // the low nibble of a byte is in wr_lo
  reg  [3:0] wr_lo;
  reg        rd_hi;   // uo_out[3:0] shows the high nibble
  reg        hdrop;   // the host wrote a byte nobody could take

  wire       wr_byte = wr_edge & wr_hi;
  wire [7:0] h_byte  = {h_nib, wr_lo};
  wire       rd_byte = rd_edge & rd_hi;

  wire h_push0  = wr_byte & (h_port == 2'd0);
  wire h_push3  = wr_byte & (h_port == 2'd1);
  wire ld_valid = wr_byte & (h_port == 2'd2);

  wire       h_room0, h_room3, ld_ready, running, overflow;
  wire       st_room0, st_room3, st_has1, st_has2;
  wire [7:0] st_head1, st_head2;

  // A pop is only asked for a byte the host has been shown, so it always gets that byte.
  wire h_pop1 = rd_byte & (h_port == 2'd0) & st_has1;
  wire h_pop2 = rd_byte & (h_port == 2'd1) & st_has2;

  always @(posedge clk or negedge rst_i) begin
    if (!rst_i) begin
      wr_hi <= 1'b0;
      wr_lo <= 4'd0;
      rd_hi <= 1'b0;
      hdrop <= 1'b0;
    end else begin
      if (wr_edge) begin
        wr_hi <= ~wr_hi;
        if (!wr_hi) wr_lo <= h_nib;
      end
      if (rd_edge) rd_hi <= ~rd_hi;
      if ((h_push0 & ~h_room0) | (h_push3 & ~h_room3) | (ld_valid & ~ld_ready)) hdrop <= 1'b1;
    end
  end

  reg       room, has;
  reg [7:0] head;
  always @(*) begin
    case (h_port)
      2'd0: begin
        room = st_room0;
        has  = st_has1;
        head = st_head1;
      end
      2'd1: begin
        room = st_room3;
        has  = st_has2;
        head = st_head2;
      end
      2'd2: begin
        room = ld_ready;
        has  = 1'b0;
        head = 8'd0;
      end
      default: begin
        room = 1'b0;
        has  = 1'b0;
        head = 8'd0;
      end
    endcase
  end

  assign uo_out = {overflow | hdrop, running, has, room, rd_hi ? head[7:4] : head[3:0]};

  // ---- the table machine -------------------------------------------------------------------------------

  wire h_has1, h_has2;
  wire [7:0] h_q1, h_q2;

  protoemu_core core (
      .clk     (clk),
      .rst_n   (rst_i),
      .pins    (pin_s2),
      .pin_out (uio_out),
      .pin_oe  (uio_oe),
      .ld_valid(ld_valid),
      .ld_byte (h_byte),
      .ld_ready(ld_ready),
      .running (running),
      .h_push0 (h_push0),
      .h_data0 (h_byte),
      .h_room0 (h_room0),
      .h_push3 (h_push3),
      .h_data3 (h_byte),
      .h_room3 (h_room3),
      .h_pop1  (h_pop1),
      .h_has1  (h_has1),
      .h_q1    (h_q1),
      .h_pop2  (h_pop2),
      .h_has2  (h_has2),
      .h_q2    (h_q2),
      .st_room0(st_room0),
      .st_room3(st_room3),
      .st_has1 (st_has1),
      .st_has2 (st_has2),
      .st_head1(st_head1),
      .st_head2(st_head2),
      .overflow(overflow)
  );

  // The host link reads the registered FIFO view; the model-order view is for the lockstep tests.
  wire _unused = &{ena, h_has1, h_has2, h_q1, h_q2, 1'b0};

endmodule
