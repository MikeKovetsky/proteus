/*
 * Copyright (c) 2026 Mike Kovetsky
 * SPDX-License-Identifier: Apache-2.0
 *
 * protoemu_trace: 32-entry hardware pin-change logic analyzer on Host Port 3.
 *
 * Passively snoops the synchronized protocol pins `pins[7:0]` (P0..P7) while the chip is running
 * (`enable == 1`). Each recorded event is 16 bits: {delta[7:0], pins[7:0]}, read by the host as two
 * bytes on Port 3 (pins first, then delta). If 255 clocks pass without a masked pin change while
 * recording, a heartbeat event {8'hFF, pins} is logged so cycle timestamps remain exact.
 *
 * Port 3 write commands (active only when `enable == 1`):
 *   0x00  stop recording (captured events remain readable)
 *   0x01  clear buffer and start recording immediately
 *   0x02  clear buffer and arm trigger (starts recording on first change of `pins & mask`)
 *   0x03  next byte written sets `mask[7:0]` (resets to 8'hFF)
 */

`default_nettype none

module protoemu_trace (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       enable,
    input  wire [7:0] pins,
    input  wire       cmd_valid,
    input  wire [7:0] cmd_byte,
    input  wire       rd_pop,
    output wire       has_byte,
    output wire [7:0] head_byte
);

  localparam [1:0] M_STOP = 2'd0, M_RUN = 2'd1, M_ARM = 2'd2;

  reg [15:0] mem [0:31];
  reg [4:0]  wr_ptr, rd_ptr;
  reg [5:0]  cnt;
  reg        rd_phase;    // 0: pins byte, 1: delta byte
  reg [1:0]  mode;
  reg        expect_mask;
  reg [7:0]  mask;
  reg [7:0]  prev_pins;
  reg [7:0]  delta;

  assign has_byte  = cnt != 6'd0;
  wire [15:0] head_ev = mem[rd_ptr];
  assign head_byte = rd_phase ? head_ev[15:8] : head_ev[7:0];

  wire take_cmd = enable & cmd_valid;
  wire cmd_clear = take_cmd & ~expect_mask & ((cmd_byte == 8'h01) | (cmd_byte == 8'h02));

  wire changed   = ((pins ^ prev_pins) & mask) != 8'd0;
  wire trig_fire = enable & ~cmd_clear & (mode == M_ARM) & changed;
  wire run_event = enable & ~cmd_clear & (mode == M_RUN) & (changed | (delta == 8'hFF));
  wire want_push = trig_fire | run_event;

  wire ev_pop  = enable & ~cmd_clear & rd_pop & has_byte & rd_phase;
  wire can_push = (cnt < 6'd32) | ev_pop;
  wire ev_push = want_push & can_push;

  wire [15:0] push_ev = trig_fire ? {8'd0, pins} : {delta, pins};

  integer i;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (i = 0; i < 32; i = i + 1) mem[i] <= 16'd0;
      wr_ptr      <= 5'd0;
      rd_ptr      <= 5'd0;
      cnt         <= 6'd0;
      rd_phase    <= 1'b0;
      mode        <= M_STOP;
      expect_mask <= 1'b0;
      mask        <= 8'hFF;
      prev_pins   <= 8'd0;
      delta       <= 8'd0;
    end else if (enable) begin
      if (take_cmd) begin
        if (expect_mask) begin
          mask        <= cmd_byte;
          expect_mask <= 1'b0;
        end else begin
          case (cmd_byte)
            8'h00: mode <= M_STOP;
            8'h01: begin
              mode      <= M_RUN;
              wr_ptr    <= 5'd0;
              rd_ptr    <= 5'd0;
              cnt       <= 6'd0;
              rd_phase  <= 1'b0;
              prev_pins <= pins;
              delta     <= 8'd1;
            end
            8'h02: begin
              mode      <= M_ARM;
              wr_ptr    <= 5'd0;
              rd_ptr    <= 5'd0;
              cnt       <= 6'd0;
              rd_phase  <= 1'b0;
              prev_pins <= pins;
              delta     <= 8'd0;
            end
            8'h03: expect_mask <= 1'b1;
            default: ;
          endcase
        end
      end

      if (!cmd_clear) begin
        // Capture path
        if (trig_fire) begin
          mode      <= M_RUN;
          prev_pins <= pins;
          delta     <= 8'd1;
        end else if (mode == M_RUN) begin
          if (changed | (delta == 8'hFF)) begin
            prev_pins <= pins;
            delta     <= 8'd1;
          end else begin
            delta <= delta + 8'd1;
          end
        end

        // FIFO write / read pointers and count
        if (ev_push) begin
          mem[wr_ptr] <= push_ev;
          wr_ptr      <= wr_ptr + 5'd1;
        end
        if (rd_pop & has_byte) begin
          rd_phase <= ~rd_phase;
          if (rd_phase) rd_ptr <= rd_ptr + 5'd1;
        end
        case ({ev_push, ev_pop})
          2'b10: cnt <= cnt + 6'd1;
          2'b01: cnt <= cnt - 6'd1;
          default: ;
        endcase
      end
    end
  end

endmodule
