/*
 * Copyright (c) 2026 Mike Kovetsky
 * SPDX-License-Identifier: Apache-2.0
 *
 * Tang Nano 9K top-level wrapper for unmodified `tt_um_mikekovetsky_proteus`.
 *
 * - Generates exact 60.000 MHz system clock from the onboard 27 MHz crystal via `proteus_pll`
 *   (or passes `clk_27m` straight through when `-DFPGA_CLK_27M` is defined for relaxed debugging).
 * - Exposes all 24 Tiny Tapeout pins on confirmed 3.3V (Bank 1 / Bank 2) expansion headers.
 * - Maps the 6 onboard active-low LEDs (Bank 3, 1.8V) to stretched live telemetry so microsecond
 *   protocol bursts and hardware kill-switch events are clearly visible to eye and camera.
 * - Maps onboard Button S1 (pin 4) to reset (ANDed with external 3.3V `ext_rst_n` on pin 63)
 *   and Button S2 (pin 3) to a live hardware KILL-SWITCH override on P4 (`uio[4]`).
 */

`default_nettype none

module tang_nano_9k_top (
    input  wire       clk_27m,
    input  wire       btn_rst_n,   // Onboard S1 (active-low, 1.8V Bank 3)
    input  wire       btn_kill_n,  // Onboard S2 (active-low, 1.8V Bank 3): forces P4 low when held
    input  wire       ext_rst_n,   // External 3.3V reset from Pico 2 (pulled up internally)
    input  wire [7:0] ui_in,       // Host nibble link inputs  (3.3V Bank 1/2)
    output wire [7:0] uo_out,      // Host nibble link outputs (3.3V Bank 1)
    inout  wire [7:0] uio,         // Protocol pins P0..P7     (3.3V Bank 2)
    output wire [5:0] led          // 6 onboard active-low LEDs (1.8V Bank 3)
);

  // ---- Clock generation (60.000 MHz via rPLL) ----------------------------------------------------------
  wire clk_sys;
  wire pll_locked;

`ifdef FPGA_CLK_27M
  assign clk_sys    = clk_27m;
  assign pll_locked = 1'b1;
`else
  proteus_pll u_pll (
      .clock_in (clk_27m),
      .clock_out(clk_sys),
      .locked   (pll_locked)
  );
`endif

  // Hold chip in reset until PLL locks and both onboard S1 and external reset are released.
  wire rst_n = pll_locked & btn_rst_n & ext_rst_n;

  // ---- Bidirectional tristate protocol pads P0..P7 -----------------------------------------------------
  wire [7:0] uio_out;
  wire [7:0] uio_oe;
  wire [7:0] uio_in;

  genvar i;
  generate
    for (i = 0; i < 8; i = i + 1) begin : gen_pad
      assign uio[i] = uio_oe[i] ? uio_out[i] : 1'bz;
    end
  endgenerate

  // Button S2 acts as an optional physical kill-switch override on P4 (active-low).
  assign uio_in[3:0] = uio[3:0];
  assign uio_in[4]   = uio[4] & btn_kill_n;
  assign uio_in[7:5] = uio[7:5];

  // ---- Unmodified Tiny Tapeout Proteus ASIC top-level --------------------------------------------------
  tt_um_mikekovetsky_proteus u_proteus (
      .ui_in  (ui_in),
      .uo_out (uo_out),
      .uio_in (uio_in),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (1'b1),
      .clk    (clk_sys),
      .rst_n  (rst_n)
  );

  // ---- Onboard 6-LED visual telemetry (active-low, ~40 ms pulse stretchers @ 60 MHz) -------------------
  // 2^21 clocks @ 60 MHz ~= 35 ms visible flash per microsecond wire burst.
  reg [25:0] heartbeat;
  reg [21:0] stretch_host;
  reg [21:0] stretch_rx;
  reg [21:0] stretch_tx;

  reg [1:0]  strobe_prev;
  reg [3:0]  wire_prev;
  reg        kill_sync1, kill_sync2;

  wire [3:0] wire_eff = (uio_oe[3:0] & uio_out[3:0]) | (~uio_oe[3:0] & uio_in[3:0]);

  always @(posedge clk_sys or negedge rst_n) begin
    if (!rst_n) begin
      heartbeat    <= 26'd0;
      stretch_host <= 22'd0;
      stretch_rx   <= 22'd0;
      stretch_tx   <= 22'd0;
      strobe_prev  <= 2'd0;
      wire_prev    <= 4'd0;
      kill_sync1   <= 1'b1;
      kill_sync2   <= 1'b1;
    end else begin
      heartbeat   <= heartbeat + 26'd1;
      strobe_prev <= ui_in[7:6];
      wire_prev   <= wire_eff;
      kill_sync1  <= uio_in[4];
      kill_sync2  <= kill_sync1;

      if (ui_in[7:6] != strobe_prev)
        stretch_host <= 22'h3FFFFF;
      else if (stretch_host != 22'd0)
        stretch_host <= stretch_host - 22'd1;

      if (wire_eff[1:0] != wire_prev[1:0])
        stretch_rx <= 22'h3FFFFF;
      else if (stretch_rx != 22'd0)
        stretch_rx <= stretch_rx - 22'd1;

      if (wire_eff[3:2] != wire_prev[3:2])
        stretch_tx <= 22'h3FFFFF;
      else if (stretch_tx != 22'd0)
        stretch_tx <= stretch_tx - 22'd1;
    end
  end

  wire running  = uo_out[6];
  wire overflow = uo_out[7];

  // Active-low LEDs on Tang Nano 9K (0 = ON, 1 = OFF):
  //   LED0 (pin 10): Solid ON when running; ~1 Hz heartbeat blink when loaded/idle
  //   LED1 (pin 11): Host nibble-link activity
  //   LED2 (pin 13): Protocol P0/P1 activity (RX / SPI SCK+MOSI / I2C SCL+SDA)
  //   LED3 (pin 14): Protocol P2/P3 activity (TX / Rate-Limit / SPI MISO+CS)
  //   LED4 (pin 15): Hardware KILL / nRESET active (P4 low or S2 pressed)
  //   LED5 (pin 16): Overflow / Host drop flag (uo_out[7])
  assign led[0] = ~(running ? 1'b1 : heartbeat[25]);
  assign led[1] = ~(stretch_host != 22'd0);
  assign led[2] = ~(stretch_rx   != 22'd0);
  assign led[3] = ~(stretch_tx   != 22'd0);
  assign led[4] = ~(~kill_sync2);
  assign led[5] = ~overflow;

endmodule

`default_nettype wire
